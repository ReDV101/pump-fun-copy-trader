#!/usr/bin/env python3
"""
Pump.fun copy-trading bot using PumpPortal WebSockets and HTTP requests.

The bot is intentionally dry-run by default. It:
  1. Subscribes to one wallet's PumpPortal account-trade events.
  2. Copies buy/sell events using a configurable fraction.
  3. Uses PumpPortal's /api/trade-local endpoint for live trades.
  4. Signs the returned transaction locally and submits it to Solana RPC.

Install:
    python -m pip install -r requirements.txt

Dry-run:
    WATCHED_WALLET=<wallet> python pump_copy_trader.py

Record events for a later backtest:
    WATCHED_WALLET=<wallet> python pump_copy_trader.py --record trades.jsonl

Backtest recorded JSONL or a JSON array:
    python pump_copy_trader.py --backtest trades.jsonl

Live mode:
    WATCHED_WALLET=<wallet> PRIVATE_KEY_BASE58=<base58-key> \
      python pump_copy_trader.py --live

Important:
  - Never put a private key in this file or commit it to source control.
  - Use a dedicated wallet with only the funds you are willing to risk.
  - Live mode is not investment advice and can lose funds quickly.
  - Paper PnL uses PAPER_SOL_PRICE as a configurable SOL-to-paper-currency
    assumption; it is not a live valuation.
  - The daily cap is held in memory and resets when the process restarts.
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


@http_app.get("/")
def health_check() -> tuple[Any, int]:
    return jsonify(
        {
            "status": "ok",
            "service": "pump-copy-trader",
            "message": "Trader process is alive",
        }
    ), 200


@http_app.get("/healthz")
def healthz() -> tuple[Any, int]:
    return jsonify({"status": "ok"}), 200


def start_http_server() -> threading.Thread:
    """Run a small health endpoint without blocking the trader loop."""
    try:
        port = int(os.getenv("PORT", "8080"))
    except ValueError as exc:
        raise ValueError("PORT must be an integer") from exc

    def serve() -> None:
        http_app.run(
            host="0.0.0.0",
            port=port,
            debug=False,
            use_reloader=False,
            threaded=True,
        )

    server_thread = threading.Thread(
        target=serve,
        name="http-health-server",
        daemon=True,
    )
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

        config = cls(
            watched_wallet=watched_wallet,
            rpc_url=os.getenv("SOLANA_RPC_URL", DEFAULT_SOLANA_RPC_URL),
            copy_fraction=env_float("COPY_FRACTION", 0.10),
            max_copy_sol=env_float("MAX_COPY_SOL", 0.05),
            max_daily_sol=env_float("MAX_DAILY_SOL", 0.50),
            min_buy_sol=env_float("MIN_BUY_SOL", 0.0005),
            slippage_percent=env_float("SLIPPAGE_PERCENT", 10.0),
            priority_fee_sol=env_float("PRIORITY_FEE_SOL", 0.0005),
            pool=os.getenv("PUMP_POOL", "auto"),
            reconnect_seconds=env_float("RECONNECT_SECONDS", 5.0),
            event_cooldown_seconds=env_float("EVENT_COOLDOWN_SECONDS", 2.0),
        )

        if not 0 < config.copy_fraction <= 1:
            raise ValueError("COPY_FRACTION must be greater than 0 and at most 1")
        if config.max_copy_sol <= 0 or config.max_daily_sol <= 0:
            raise ValueError("MAX_COPY_SOL and MAX_DAILY_SOL must be positive")
        if config.slippage_percent <= 0:
            raise ValueError("SLIPPAGE_PERCENT must be positive")
        return config


class DailyBudget:
    """In-memory SOL spend limiter for copied buys."""

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


def format_paper_money(value: float) -> str:
    """Display the same simulated balance in the user's requested currencies."""
    return f"${value:,.2f} (€{value:,.2f})"


class PaperPortfolio:
    """
    Small mark-to-market paper account.

    The account is denominated in paper dollars/euros. Since PumpPortal events
    contain SOL and token quantities rather than fiat prices, PAPER_SOL_PRICE
    converts SOL notional into the simulated cash balance.
    """

    def __init__(
        self,
        initial_balance: float = DEFAULT_PAPER_BALANCE,
        sol_price: float = DEFAULT_PAPER_SOL_PRICE,
        fee_bps: float = 0.0,
    ) -> None:
        if initial_balance <= 0:
            raise ValueError("PAPER_INITIAL_BALANCE must be positive")
        if sol_price <= 0:
            raise ValueError("PAPER_SOL_PRICE must be positive")
        if fee_bps < 0:
            raise ValueError("PAPER_FEE_BPS cannot be negative")

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
            price_sol = self.last_prices_sol.get(
                mint, self.average_cost_sol.get(mint, 0.0)
            )
            holdings_value += quantity * price_sol * self.sol_price
        return self.cash_balance + holdings_value

    def apply_trade(self, trade: dict[str, Any]) -> bool:
        """Apply one simulated copy trade and print its balance/PnL."""
        action = str(trade["action"])
        mint = str(trade["mint"])
        amount = float(trade["amount"])
        source_sol = float(trade.get("source_sol", 0) or 0)
        source_tokens = float(trade.get("source_tokens", 0) or 0)
        if action not in {"buy", "sell"} or amount <= 0:
            return False
        if source_sol <= 0 or source_tokens <= 0:
            LOG.warning("Paper trade skipped: source price is unavailable for %s", mint)
            return False

        price_sol = source_sol / source_tokens
        if price_sol <= 0:
            return False

        before_balance = self.total_balance()
        self.last_prices_sol[mint] = price_sol
        previous_quantity = self.positions.get(mint, 0.0)
        previous_average_cost = self.average_cost_sol.get(mint, price_sol)

        if action == "buy":
            token_quantity = amount / price_sol
            gross_cost = amount * self.sol_price
            fee = gross_cost * self.fee_rate
            total_cost = gross_cost + fee
            if total_cost > self.cash_balance:
                LOG.warning(
                    "Paper buy skipped: need %s, cash is %s",
                    format_paper_money(total_cost),
                    format_paper_money(self.cash_balance),
                )
                return False

            new_quantity = previous_quantity + token_quantity
            self.average_cost_sol[mint] = (
                (previous_quantity * previous_average_cost)
                + (token_quantity * price_sol)
            ) / new_quantity
            self.positions[mint] = new_quantity
            self.cash_balance -= total_cost
            detail = (
                f"spent {format_paper_money(total_cost)}; "
                f"received {token_quantity:.6g} tokens"
            )
        else:
            available_quantity = self.positions.get(mint, 0.0)
            token_quantity = min(amount, available_quantity)
            if token_quantity <= 0:
                LOG.info("Paper sell skipped: no %s position to sell", mint)
                return False

            gross_proceeds = token_quantity * price_sol * self.sol_price
            fee = gross_proceeds * self.fee_rate
            net_proceeds = gross_proceeds - fee
            cost_value = token_quantity * previous_average_cost * self.sol_price
            realized_pnl = net_proceeds - cost_value
            self.cash_balance += net_proceeds
            remaining_quantity = available_quantity - token_quantity
            if remaining_quantity <= 1e-12:
                self.positions.pop(mint, None)
                self.average_cost_sol.pop(mint, None)
            else:
                self.positions[mint] = remaining_quantity
            detail = (
                f"received {format_paper_money(net_proceeds)}; "
                f"realized PnL {format_paper_money(realized_pnl)}"
            )

        after_balance = self.total_balance()
        trade_pnl = after_balance - before_balance
        total_pnl = after_balance - self.initial_balance
        self.executed_trades += 1
        LOG.warning(
            "PAPER %s %s | %s | trade PnL %+.2f | total balance %s | "
            "total PnL %+.2f",
            action.upper(),
            mint,
            detail,
            trade_pnl,
            format_paper_money(after_balance),
            total_pnl,
        )
        return True

    def print_summary(self, label: str = "Paper account") -> None:
        final_balance = self.total_balance()
        total_pnl = final_balance - self.initial_balance
        return_pct = (total_pnl / self.initial_balance) * 100
        LOG.warning(
            "%s | trades=%d | final balance=%s | total PnL=%+.2f | return=%+.2f%%",
            label,
            self.executed_trades,
            format_paper_money(final_balance),
            total_pnl,
            return_pct,
        )


def prepare_trade(
    event: dict[str, Any],
    config: Config,
    budget: DailyBudget,
) -> dict[str, Any] | None:
    """Convert a PumpPortal event into a capped copy-trade decision."""
    event_type = str(event.get("txType", "")).lower()
    mint = str(event.get("mint", "")).strip()
    signature = str(event.get("signature", "")).strip() or "historical-event"
    if event_type not in {"buy", "sell"} or not mint:
        return None

    try:
        source_sol = float(event.get("solAmount", 0) or 0)
        source_tokens = float(event.get("tokenAmount", 0) or 0)
    except (TypeError, ValueError):
        LOG.warning("Skipping malformed event: %s", event)
        return None

    if event_type == "buy":
        if source_sol < config.min_buy_sol:
            LOG.info("Skipping small buy %s: %.9f SOL", signature[:12], source_sol)
            return None

        requested_sol = source_sol * config.copy_fraction
        amount_sol = min(
            requested_sol,
            config.max_copy_sol,
            budget.remaining(),
        )
        if amount_sol <= 0 or not budget.reserve(amount_sol):
            LOG.warning("Daily SOL cap reached; skipping buy for %s", mint)
            return None

        trade = {
            "action": "buy",
            "mint": mint,
            "amount": amount_sol,
            "denominatedInSol": "true",
        }
    else:
        if source_tokens <= 0:
            LOG.info("Skipping sell with no token amount: %s", signature[:12])
            return None
        trade = {
            "action": "sell",
            "mint": mint,
            "amount": source_tokens * config.copy_fraction,
            "denominatedInSol": "false",
        }

    trade.update(
        {
            "source_signature": signature,
            "source_sol": source_sol,
            "source_tokens": source_tokens,
            "slippage": config.slippage_percent,
            "priorityFee": config.priority_fee_sol,
            "pool": event.get("pool") or config.pool,
        }
    )
    return trade


class PumpPortalTrader:
    """Builds and optionally submits PumpPortal local trade transactions."""

    def __init__(self, config: Config, live: bool, paper: PaperPortfolio) -> None:
        self.config = config
        self.live = live
        self.paper = paper
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": "pump-copy-trader/1.0"})
        self.keypair: Any | None = None
        self.public_key: str | None = None

        if live:
            private_key = os.getenv("PRIVATE_KEY_BASE58", "").strip()
            if not private_key:
                raise ValueError(
                    "PRIVATE_KEY_BASE58 is required only when --live is used"
                )
            try:
                from solders.keypair import Keypair

                self.keypair = Keypair.from_base58_string(private_key)
                self.public_key = str(self.keypair.pubkey())
            except ImportError as exc:
                raise RuntimeError(
                    "Live mode needs solders. Install requirements.txt first."
                ) from exc
            except Exception as exc:
                raise ValueError(
                    "PRIVATE_KEY_BASE58 could not be parsed as a base58 key"
                ) from exc

            if self.public_key == self.config.watched_wallet:
                raise ValueError(
                    "The copy wallet must not be the same as WATCHED_WALLET"
                )

    def copy_event(self, event: dict[str, Any], budget: DailyBudget) -> None:
        trade = prepare_trade(event, self.config, budget)
        if trade is not None:
            self.execute_trade(trade)

    def execute_trade(self, trade: dict[str, Any]) -> None:
        action = trade["action"]
        mint = trade["mint"]
        amount = trade["amount"]
        if not self.live:
            if not self.paper.apply_trade(trade):
                return
            LOG.warning(
                "DRY RUN: %s %.9f %s of %s | source=%s",
                action.upper(),
                amount,
                "SOL" if trade["denominatedInSol"] == "true" else "tokens",
                mint,
                str(trade["source_signature"])[:12],
            )
            return

        assert self.public_key is not None
        request_body = {
            "publicKey": self.public_key,
            "action": action,
            "mint": mint,
            "amount": amount,
            "denominatedInSol": trade["denominatedInSol"],
            "slippage": trade["slippage"],
            "priorityFee": trade["priorityFee"],
            "pool": trade["pool"],
        }

        LOG.warning(
            "LIVE: requesting %s trade for %.9f %s of %s",
            action,
            amount,
            "SOL" if trade["denominatedInSol"] == "true" else "tokens",
            mint,
        )
        response = self.http.post(
            PUMPPORTAL_TRADE_URL,
            json=request_body,
            timeout=30,
        )
        response.raise_for_status()
        if not response.content:
            raise RuntimeError("PumpPortal returned an empty transaction")
        signature = self.sign_and_send(response.content)
        self.paper.apply_trade(trade)
        LOG.warning("Submitted copied %s: %s", action, signature)

    def sign_and_send(self, transaction_bytes: bytes) -> str:
        """Sign PumpPortal's serialized transaction and submit it to Solana RPC."""
        if self.keypair is None:
            raise RuntimeError("A keypair is required to submit live trades")

        from solders.message import to_bytes_versioned
        from solders.transaction import VersionedTransaction

        unsigned = VersionedTransaction.from_bytes(transaction_bytes)
        signature = self.keypair.sign_message(to_bytes_versioned(unsigned.message))
        signed = VersionedTransaction.populate(unsigned.message, [signature])
        encoded = base64.b64encode(bytes(signed)).decode("ascii")

        rpc_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                encoded,
                {
                    "encoding": "base64",
                    "skipPreflight": False,
                    "preflightCommitment": "processed",
                },
            ],
        }
        response = self.http.post(self.config.rpc_url, json=rpc_body, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(f"Solana RPC rejected transaction: {payload['error']}")
        return str(payload["result"])


class CopyTrader:
    def __init__(
        self,
        config: Config,
        live: bool,
        paper_balance: float | None = None,
        paper_sol_price: float | None = None,
        paper_fee_bps: float | None = None,
        record_path: str | None = None,
    ) -> None:
        self.config = config
        self.paper = PaperPortfolio(
            initial_balance=(
                paper_balance
                if paper_balance is not None
                else env_float("PAPER_INITIAL_BALANCE", DEFAULT_PAPER_BALANCE)
            ),
            sol_price=(
                paper_sol_price
                if paper_sol_price is not None
                else env_float("PAPER_SOL_PRICE", DEFAULT_PAPER_SOL_PRICE)
            ),
            fee_bps=(
                paper_fee_bps
                if paper_fee_bps is not None
                else env_float("PAPER_FEE_BPS", 0.0)
            ),
        )
        self.trader = PumpPortalTrader(config, live, self.paper)
        self.budget = DailyBudget(config.max_daily_sol)
        self.stop_event = asyncio.Event()
        self.seen_signatures: deque[str] = deque(maxlen=2000)
        self.last_mint_event: dict[tuple[str, str], float] = {}
        self.record_path = Path(record_path) if record_path else None

    async def run(self) -> None:
        LOG.info(
            "Watching %s | mode=%s | copy_fraction=%.2f | max_copy=%.4f SOL "
            "| daily_cap=%.4f SOL",
            self.config.watched_wallet,
            "LIVE" if self.trader.live else "DRY RUN",
            self.config.copy_fraction,
            self.config.max_copy_sol,
            self.config.max_daily_sol,
        )

        try:
            while not self.stop_event.is_set():
                try:
                    await self.watch_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception(
                        "WebSocket loop failed; reconnecting in %.1f seconds",
                        self.config.reconnect_seconds,
                    )
                    await self.wait_or_stop(self.config.reconnect_seconds)
        finally:
            self.paper.print_summary()

    async def watch_once(self) -> None:
        async with websockets.connect(
            PUMPPORTAL_WS_URL,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=2**20,
        ) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "method": "subscribeAccountTrade",
                        "keys": [self.config.watched_wallet],
                    }
                )
            )
            LOG.info("Subscribed to account trades")

            async for raw_message in websocket:
                if self.stop_event.is_set():
                    return
                await self.handle_message(raw_message)

    async def handle_message(self, raw_message: str | bytes) -> None:
        try:
            decoded = raw_message.decode() if isinstance(raw_message, bytes) else raw_message
            message = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            LOG.debug("Ignoring non-JSON WebSocket message")
            return

        if not isinstance(message, dict):
            return
        if message.get("error"):
            LOG.error("PumpPortal subscription error: %s", message["error"])
            return

        signature = str(message.get("signature", "")).strip()
        if not signature or signature in self.seen_signatures:
            return
        self.seen_signatures.append(signature)

        event_type = str(message.get("txType", "")).lower()
        mint = str(message.get("mint", "")).strip()
        if event_type not in {"buy", "sell"} or not mint:
            return

        now = time.monotonic()
        key = (event_type, mint)
        last_seen = self.last_mint_event.get(key, 0.0)
        if now - last_seen < self.config.event_cooldown_seconds:
            LOG.info("Cooldown: skipping %s for %s", event_type, mint)
            return
        self.last_mint_event[key] = now

        LOG.info(
            "Observed %s: mint=%s source_sol=%s source_tokens=%s tx=%s",
            event_type,
            mint,
            message.get("solAmount"),
            message.get("tokenAmount"),
            signature[:12],
        )
        if self.record_path:
            with self.record_path.open("a", encoding="utf-8") as history:
                json.dump(message, history, separators=(",", ":"))
                history.write("\n")
        try:
            self.trader.copy_event(message, self.budget)
        except requests.RequestException:
            LOG.exception("HTTP request failed while copying %s", signature[:12])
        except Exception:
            LOG.exception("Could not copy source trade %s", signature[:12])

    async def wait_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def stop(self) -> None:
        self.stop_event.set()


def load_historical_events(path: Path) -> list[dict[str, Any]]:
    """Load PumpPortal event objects from JSONL or a JSON array/object."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []

    if path.suffix.lower() == ".json":
        payload = json.loads(text)
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            if isinstance(payload.get("events"), list):
                return [
                    item for item in payload["events"] if isinstance(item, dict)
                ]
            return [payload]
        return []

    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            LOG.warning("Skipping invalid JSON on line %d: %s", line_number, exc)
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def run_backtest(
    history_path: str,
    paper_balance: float | None = None,
    paper_sol_price: float | None = None,
    paper_fee_bps: float | None = None,
) -> None:
    """Replay recorded PumpPortal events through the paper account."""
    path = Path(history_path)
    if not path.exists():
        raise FileNotFoundError(f"Historical file not found: {path}")

    config = Config.from_environment(require_wallet=False)
    paper = PaperPortfolio(
        initial_balance=(
            paper_balance
            if paper_balance is not None
            else env_float("PAPER_INITIAL_BALANCE", DEFAULT_PAPER_BALANCE)
        ),
        sol_price=(
            paper_sol_price
            if paper_sol_price is not None
            else env_float("PAPER_SOL_PRICE", DEFAULT_PAPER_SOL_PRICE)
        ),
        fee_bps=(
            paper_fee_bps
            if paper_fee_bps is not None
            else env_float("PAPER_FEE_BPS", 0.0)
        ),
    )
    budget = DailyBudget(config.max_daily_sol)
    events = load_historical_events(path)
    simulated = 0

    for event in events:
        trade = prepare_trade(event, config, budget)
        if trade is not None and paper.apply_trade(trade):
            simulated += 1

    LOG.warning(
        "Backtest complete | input events=%d | simulated trades=%d | file=%s",
        len(events),
        simulated,
        path,
    )
    paper.print_summary("Backtest paper account")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually sign and submit trades; default is dry-run",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    parser.add_argument(
        "--record",
        metavar="PATH",
        help="Append observed buy/sell events as JSONL for later backtesting",
    )
    parser.add_argument(
        "--backtest",
        metavar="PATH",
        help="Replay a JSONL or JSON historical event file instead of connecting",
    )
    parser.add_argument(
        "--paper-balance",
        type=float,
        default=None,
        help="Initial paper balance in dollars/euros (default: 100)",
    )
    parser.add_argument(
        "--paper-sol-price",
        type=float,
        default=None,
        help="Assumed SOL price in paper dollars/euros (default: 100)",
    )
    parser.add_argument(
        "--paper-fee-bps",
        type=float,
        default=None,
        help="Paper fee in basis points (default: 0)",
    )
    args = parser.parse_args()
    if args.live and args.backtest:
        parser.error("--live cannot be used with --backtest")
    if args.record and args.backtest:
        parser.error("--record cannot be used with --backtest")
    return args


async def async_main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    start_http_server()

    if args.backtest:
        run_backtest(
            args.backtest,
            paper_balance=args.paper_balance,
            paper_sol_price=args.paper_sol_price,
            paper_fee_bps=args.paper_fee_bps,
        )
        return

    config = Config.from_environment()
    app = CopyTrader(
        config,
        live=args.live,
        paper_balance=args.paper_balance,
        paper_sol_price=args.paper_sol_price,
        paper_fee_bps=args.paper_fee_bps,
        record_path=args.record,
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.stop)
        except NotImplementedError:
            # Signal handlers are not available on a few event-loop platforms.
            pass

    await app.run()


if __name__ == "__main__":
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass
