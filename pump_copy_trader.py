#!/usr/bin/env python3
"""
Pump.fun copy-trading bot with Telegram Notifications & PumpPortal WebSockets.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import requests
import websockets
from flask import Flask, jsonify

LOG = logging.getLogger("pump-copy-trader")

PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"
PUMPPORTAL_TRADE_URL = "https://pumpportal.fun/api/trade-local"
DEFAULT_SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
DEFAULT_PAPER_BALANCE = 100.0
DEFAULT_PAPER_SOL_PRICE = 100.0

http_app = Flask("pump-copy-trader-health")


def send_telegram_message(message: str) -> None:
    """Send alert to Telegram Bot if credentials exist."""
    token = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
    
    if not token or not chat_id:
        return
        
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        LOG.error("Failed to send Telegram message: %s", e)


@http_app.get("/")
def health_check() -> tuple[Any, int]:
    return jsonify({"status": "ok", "service": "pump-copy-trader"}), 200


@http_app.get("/healthz")
def healthz() -> tuple[Any, int]:
    return jsonify({"status": "ok"}), 200


def start_http_server() -> threading.Thread:
    try:
        port = int(os.getenv("PORT", "8080"))
    except ValueError as exc:
        raise ValueError("PORT must be an integer") from exc

    def serve() -> None:
        http_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)

    server_thread = threading.Thread(target=serve, name="http-health-server", daemon=True)
    server_thread.start()
    LOG.info("HTTP health server starting on port %d", port)
    return server_thread


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {value!r}") from exc


@dataclass(frozen=True)
class Config:
    watched_wallet: str
    rpc_url: str
    copy_fraction: float
    max_copy_sol: float
    max_daily_sol: float
    min_buy_sol: float
    slippage_percent: float
    priority_fee_sol: float
    pool: str
    reconnect_seconds: float
    event_cooldown_seconds: float

    @classmethod
    def from_environment(cls, require_wallet: bool = True) -> "Config":
        watched_wallet = os.getenv("WATCHED_WALLET", "").strip()
        if require_wallet and not watched_wallet:
            raise ValueError("WATCHED_WALLET is required")
        if not watched_wallet:
            watched_wallet = "historical-backtest"

        return cls(
            watched_wallet=watched_wallet,
            rpc_url=os.getenv("SOLANA_RPC_URL", os.getenv("RPC_URL", DEFAULT_SOLANA_RPC_URL)),
            copy_fraction=env_float("COPY_FRACTION", 0.10),
            max_copy_sol=env_float("MAX_COPY_SOL", 0.05),
            max_daily_sol=env_float("MAX_DAILY_SOL", 0.50),
            min_buy_sol=env_float("MIN_BUY_SOL", 0.0001),
            slippage_percent=env_float("SLIPPAGE_PERCENT", 10.0),
            priority_fee_sol=env_float("PRIORITY_FEE_SOL", 0.0005),
            pool=os.getenv("PUMP_POOL", "auto"),
            reconnect_seconds=env_float("RECONNECT_SECONDS", 5.0),
            event_cooldown_seconds=env_float("EVENT_COOLDOWN_SECONDS", 1.0),
        )


class DailyBudget:
    def __init__(self, daily_limit_sol: float) -> None:
        self.daily_limit_sol = daily_limit_sol
        self.spent_sol = 0.0
        self.day = date.today()

    def remaining(self) -> float:
        self._reset_if_needed()
        return max(0.0, self.daily_limit_sol - self.spent_sol)

    def reserve(self, amount_sol: float) -> bool:
        self._reset_if_needed()
        if amount_sol <= 0 or self.spent_sol + amount_sol > self.daily_limit_sol:
            return False
        self.spent_sol += amount_sol
        return True

    def _reset_if_needed(self) -> None:
        today = date.today()
        if today != self.day:
            self.day = today
            self.spent_sol = 0.0


class PaperPortfolio:
    def __init__(self, initial_balance: float = DEFAULT_PAPER_BALANCE, sol_price: float = DEFAULT_PAPER_SOL_PRICE, fee_bps: float = 0.0) -> None:
        self.initial_balance = initial_balance
        self.cash_balance = initial_balance
        self.sol_price = sol_price
        self.fee_rate = fee_bps / 10_000
        self.positions: dict[str, float] = {}
        self.average_cost_sol: dict[str, float] = {}
        self.last_prices_sol: dict[str, float] = {}
        self.executed_trades = 0

    def total_balance(self) -> float:
        holdings_value = 0.0
        for mint, quantity in self.positions.items():
            price_sol = self.last_prices_sol.get(mint, self.average_cost_sol.get(mint, 0.0))
            holdings_value += quantity * price_sol * self.sol_price
        return self.cash_balance + holdings_value

    def apply_trade(self, trade: dict[str, Any]) -> bool:
        action = str(trade["action"])
        mint = str(trade["mint"])
        amount = float(trade["amount"])
        source_sol = float(trade.get("source_sol", 0) or 0)
        source_tokens = float(trade.get("source_tokens", 0) or 0)
        
        if action not in {"buy", "sell"} or amount <= 0 or source_sol <= 0 or source_tokens <= 0:
            return False

        price_sol = source_sol / source_tokens
        before_balance = self.total_balance()
        self.last_prices_sol[mint] = price_sol
        previous_quantity = self.positions.get(mint, 0.0)

        if action == "buy":
            token_quantity = amount / price_sol
            gross_cost = amount * self.sol_price
            total_cost = gross_cost + (gross_cost * self.fee_rate)
            if total_cost > self.cash_balance:
                return False

            new_quantity = previous_quantity + token_quantity
            self.positions[mint] = new_quantity
            self.cash_balance -= total_cost
        else:
            token_quantity = min(amount, previous_quantity)
            if token_quantity <= 0:
                return False
            net_proceeds = (token_quantity * price_sol * self.sol_price) * (1 - self.fee_rate)
            self.cash_balance += net_proceeds
            self.positions[mint] = previous_quantity - token_quantity

        after_balance = self.total_balance()
        self.executed_trades += 1
        
        msg = f"<b>DRY-RUN {action.upper()}</b>\nToken: <code>{mint}</code>\nBalance: ${after_balance:.2f}"
        send_telegram_message(msg)
        LOG.warning("PAPER %s %s | Balance: $%.2f", action.upper(), mint, after_balance)
        return True


def prepare_trade(event: dict[str, Any], config: Config, budget: DailyBudget) -> dict[str, Any] | None:
    event_type = str(event.get("txType", "")).lower()
    mint = str(event.get("mint", "")).strip()
    signature = str(event.get("signature", "")).strip() or "event"
    if event_type not in {"buy", "sell"} or not mint:
        return None

    try:
        source_sol = float(event.get("solAmount", 0) or 0)
        source_tokens = float(event.get("tokenAmount", 0) or 0)
    except (TypeError, ValueError):
        return None

    if event_type == "buy":
        amount_sol = min(source_sol * config.copy_fraction, config.max_copy_sol, budget.remaining())
        if amount_sol <= 0 or not budget.reserve(amount_sol):
            return None
        trade = {"action": "buy", "mint": mint, "amount": amount_sol, "denominatedInSol": "true"}
    else:
        trade = {"action": "sell", "mint": mint, "amount": source_tokens * config.copy_fraction, "denominatedInSol": "false"}

    trade.update({"source_signature": signature, "source_sol": source_sol, "source_tokens": source_tokens, "slippage": config.slippage_percent, "priorityFee": config.priority_fee_sol, "pool": event.get("pool") or config.pool})
    return trade


class PumpPortalTrader:
    def __init__(self, config: Config, live: bool, paper: PaperPortfolio) -> None:
        self.config = config
        self.live = live
        self.paper = paper

    def copy_event(self, event: dict[str, Any], budget: DailyBudget) -> None:
        trade = prepare_trade(event, self.config, budget)
        if trade is not None:
            if not self.live:
                self.paper.apply_trade(trade)


class CopyTrader:
    def __init__(self, config: Config, live: bool) -> None:
        self.config = config
        self.paper = PaperPortfolio()
        self.trader = PumpPortalTrader(config, live, self.paper)
        self.budget = DailyBudget(config.max_daily_sol)
        self.stop_event = asyncio.Event()
        self.seen_signatures: deque[str] = deque(maxlen=2000)

    async def run(self) -> None:
        LOG.info("Watching %s | TELEGRAM ACTIVE", self.config.watched_wallet)
        send_telegram_message(f"🚀 Bot Started Watching: <code>{self.config.watched_wallet}</code>")

        while not self.stop_event.is_set():
            try:
                await self.watch_once()
            except Exception as e:
                LOG.error("WS Loop Error: %s. Reconnecting...", e)
                await asyncio.sleep(self.config.reconnect_seconds)

    async def watch_once(self) -> None:
        async with websockets.connect(PUMPPORTAL_WS_URL, ping_interval=20, ping_timeout=20) as websocket:
            await websocket.send(json.dumps({"method": "subscribeAccountTrade", "keys": [self.config.watched_wallet]}))
            LOG.info("Subscribed to account trades")

            async for raw_message in websocket:
                if self.stop_event.is_set():
                    return
                # DEBUG PRINT: Τυπώνει τα πάντα στα logs του Render!
                LOG.info("RAW WS DATA: %s", raw_message)
                
                try:
                    message = json.loads(raw_message)
                    if isinstance(message, dict) and message.get("signature"):
                        sig = str(message["signature"])
                        if sig not in self.seen_signatures:
                            self.seen_signatures.append(sig)
                            self.trader.copy_event(message, self.budget)
                except Exception as e:
                    LOG.error("Error processing msg: %s", e)

    def stop(self) -> None:
        self.stop_event.set()


async def async_main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    start_http_server()
    config = Config.from_environment()
    app = CopyTrader(config, live=False)
    await app.run()


if __name__ == "__main__":
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


    
            
