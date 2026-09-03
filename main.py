import os
import time
import json
import requests
from flask import Flask, request, jsonify
from threading import Thread, Lock
from google import genai

app = Flask(__name__)

# ------------------------------------------------------------------
# 1. ENVIRONMENT VARIABLES
# ------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID")
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
WATCHED_WALLET = os.getenv("WATCHED_WALLET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WEBHOOK_AUTH = os.getenv("WEBHOOK_AUTH", "")

STARTING_BALANCE_SOL = float(os.getenv("STARTING_BALANCE_SOL", "10"))
PAPER_TRADE_SOL = float(os.getenv("PAPER_TRADE_SOL", "0.5"))
SLIPPAGE_PCT = float(os.getenv("SLIPPAGE_PCT", "8"))
FEE_SOL = float(os.getenv("FEE_SOL", "0.003"))

# ΝΕΟ: μέγιστο συνολικό ποσό ανά token (0 = χωρίς όριο)
MAX_PER_TOKEN_SOL = float(os.getenv("MAX_PER_TOKEN_SOL", "0"))

STATE_FILE = os.getenv("STATE_PATH", "paper_state.json")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

# ------------------------------------------------------------------
# ΔΙΟΡΘΩΣΗ: Όταν ένα pump.fun token "bonds", μετακομίζει σε Raydium.
# Οι επόμενες πωλήσεις ΔΕΝ περνάνε πια από pump.fun.
# Παλιά τις αγνοούσαμε -> οι θέσεις έμεναν ανοιχτές για πάντα.
# ------------------------------------------------------------------
DEX_SOURCES = {
    "PUMP_FUN", "PUMP_AMM", "RAYDIUM", "JUPITER", "ORCA",
    "METEORA", "PHOENIX", "LIFINITY", "ALDRIN", "SABER",
    "SERUM", "OPENBOOK", "FLUXBEAM", "MOONSHOT", "BONKSWAP",
}

IGNORED_MINTS = {
    "So11111111111111111111111111111111111111112",   # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",   # mSOL
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",  # jitoSOL
}

state_lock = Lock()
seen_signatures = set()
seen_lock = Lock()


def already_processed(signature):
    with seen_lock:
        if signature in seen_signatures:
            return True
        seen_signatures.add(signature)
        if len(seen_signatures) > 2000:
            for s in list(seen_signatures)[:1000]:
                seen_signatures.discard(s)
        return False


# ------------------------------------------------------------------
# 2. STATE
# ------------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"State load error: {e}")
    return {
        "balance_sol": STARTING_BALANCE_SOL,
        "positions": {},
        "closed_trades": [],
        "last_signature": None,
        "total_fees_sol": 0.0,
        "latencies": [],
    }


def save_state():
    """Καλείται ΠΑΝΤΑ με το state_lock κρατημένο."""
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"State save error: {e}")


state = load_state()
if "latencies" not in state:
    state["latencies"] = []


# ------------------------------------------------------------------
# 3. GEMINI
# ------------------------------------------------------------------
ai_client = None
if GEMINI_API_KEY:
    try:
        ai_client = genai.Client(api_key=GEMINI_API_KEY)
        print("Gemini AI Client initialized successfully!")
    except Exception as e:
        print(f"Gemini Init Error: {e}")


# ------------------------------------------------------------------
# 4. TELEGRAM HELPERS
# ------------------------------------------------------------------
def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[No Telegram configured] {text}")
        return
    if len(text) > 4000:
        text = text[:3990] + "\n[...]"
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"Telegram send error: {e}")


def clear_telegram_webhooks():
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook?drop_pending_updates=True"
        requests.get(url, timeout=10)
    except Exception as e:
        print(f"Webhook reset warning: {e}")


def short_mint(mint):
    return f"{mint[:4]}...{mint[-4:]}" if mint and len(mint) > 8 else mint


# ------------------------------------------------------------------
# 5. ΤΙΜΗ TOKEN
# ------------------------------------------------------------------
def get_current_price_sol(mint):
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            pairs = res.json().get("pairs") or []
            for p in pairs:
                if p.get("chainId") == "solana" and p.get("priceNative"):
                    return float(p["priceNative"])
    except Exception as e:
        print(f"Price fetch error: {e}")
    return None


# ------------------------------------------------------------------
# 6. PAPER TRADING
# ------------------------------------------------------------------
def paper_buy(mint, wallet_token_amount, wallet_sol_spent, signature,
              tx_time=None, via="webhook"):
    if not wallet_token_amount or not wallet_sol_spent:
        return

    latency = None
    if tx_time:
        latency = time.time() - float(tx_time)

    wallet_price = wallet_sol_spent / wallet_token_amount
    live_price = get_current_price_sol(mint)
    if live_price is None:
        live_price = wallet_price
    entry_price = live_price * (1 + SLIPPAGE_PCT / 100.0)
    if entry_price <= 0:
        return

    if wallet_price > 0 and (entry_price / wallet_price > 50 or
                             entry_price / wallet_price < 0.02):
        print(f"Skipping {mint}: αφύσικη διαφορά τιμής")
        return

    with state_lock:
        total_cost = PAPER_TRADE_SOL + FEE_SOL
        if state["balance_sol"] < total_cost:
            send_telegram_message(
                f"⚠️ Ανεπαρκές virtual balance για {short_mint(mint)}"
            )
            return

        pos = state["positions"].get(
            mint, {"amount": 0.0, "avg_price_sol": 0.0, "opened_at": time.time(),
                   "invested_sol": 0.0}
        )

        # Όριο ανά token
        if MAX_PER_TOKEN_SOL > 0:
            already = pos.get("invested_sol", 0.0)
            if already + PAPER_TRADE_SOL > MAX_PER_TOKEN_SOL:
                send_telegram_message(
                    f"⏸ Παράλειψη αγοράς {short_mint(mint)} — "
                    f"όριο {MAX_PER_TOKEN_SOL:.2f} SOL ανά token "
                    f"(ήδη {already:.2f})"
                )
                return

        tokens_bought = PAPER_TRADE_SOL / entry_price
        cost_before = pos["amount"] * pos["avg_price_sol"]
        new_amount = pos["amount"] + tokens_bought
        new_avg_price = (cost_before + PAPER_TRADE_SOL) / new_amount

        state["positions"][mint] = {
            "amount": new_amount,
            "avg_price_sol": new_avg_price,
            "opened_at": pos.get("opened_at", time.time()),
            "entry_latency": latency,
            "entry_via": via,
            "invested_sol": pos.get("invested_sol", 0.0) + PAPER_TRADE_SOL,
            "buys": pos.get("buys", 0) + 1,
        }
        state["balance_sol"] -= total_cost
        state["total_fees_sol"] += FEE_SOL
        if latency is not None:
            state["latencies"].append(round(latency, 2))
            state["latencies"] = state["latencies"][-500:]
        save_state()
        balance_now = state["balance_sol"]
        buys_count = state["positions"][mint]["buys"]

    slip_vs_wallet = (entry_price / wallet_price - 1) * 100 if wallet_price else 0
    lat_line = f"⏱ Καθυστέρηση: {latency:.1f}s ({via})\n" if latency is not None else ""

    send_telegram_message(
        f"🟢 <b>PAPER BUY</b> #{buys_count}\n"
        f"Token: <code>{short_mint(mint)}</code>\n"
        f"{lat_line}"
        f"Ποσό: {PAPER_TRADE_SOL:.3f} SOL (+{FEE_SOL:.4f} fee)\n"
        f"Τιμή wallet: {wallet_price:.8f}\n"
        f"Δικό μας entry: {entry_price:.8f} ({slip_vs_wallet:+.1f}%)\n"
        f"Balance: {balance_now:.3f} SOL"
    )


def close_position(mint, exit_price, ratio=1.0, closed_by="wallet",
                   latency=None, notify=True):
    """
    Κοινή λογική κλεισίματος.
    closed_by: "wallet" (ακολουθήσαμε τον trader) ή "manual" (το έκλεισες εσύ)
    """
    with state_lock:
        pos = state["positions"].get(mint)
        if not pos or pos["amount"] <= 0:
            return None

        ratio = max(0.0, min(1.0, ratio))
        tokens_sold = pos["amount"] * ratio
        if tokens_sold <= 0:
            return None

        proceeds = tokens_sold * exit_price - FEE_SOL
        cost_basis = tokens_sold * pos["avg_price_sol"]
        pnl = proceeds - cost_basis
        pnl_pct = (pnl / cost_basis * 100) if cost_basis else 0.0
        held_min = (time.time() - pos.get("opened_at", time.time())) / 60

        state["balance_sol"] += proceeds
        state["total_fees_sol"] += FEE_SOL
        state["closed_trades"].append({
            "mint": mint,
            "pnl_sol": pnl,
            "pnl_pct": pnl_pct,
            "held_minutes": held_min,
            "entry_latency": pos.get("entry_latency"),
            "exit_latency": latency,
            "closed_by": closed_by,
            "timestamp": time.time(),
        })

        remaining = pos["amount"] - tokens_sold
        if remaining > 1e-9:
            state["positions"][mint]["amount"] = remaining
            state["positions"][mint]["invested_sol"] = (
                pos.get("invested_sol", 0.0) * (1 - ratio)
            )
        else:
            del state["positions"][mint]

        if latency is not None:
            state["latencies"].append(round(latency, 2))
            state["latencies"] = state["latencies"][-500:]

        save_state()
        balance_now = state["balance_sol"]

    if notify:
        emoji = "✅" if pnl >= 0 else "❌"
        by_label = "📋 ακολούθησα τον trader" if closed_by == "wallet" else "✋ χειροκίνητο"
        lat_line = f"⏱ Καθυστέρηση: {latency:.1f}s\n" if latency is not None else ""
        send_telegram_message(
            f"🔴 <b>PAPER SELL</b> {emoji}\n"
            f"Token: <code>{short_mint(mint)}</code>\n"
            f"{by_label}\n"
            f"{lat_line}"
            f"Πουλήθηκε: {ratio*100:.0f}% της θέσης\n"
            f"P&L: {pnl:+.4f} SOL ({pnl_pct:+.1f}%)\n"
            f"Κρατήθηκε: {held_min:.0f} λεπτά\n"
            f"Balance: {balance_now:.3f} SOL"
        )

    return pnl


def paper_sell(mint, wallet_token_amount, wallet_sol_received, signature,
               tx_time=None, sell_ratio=1.0, via="webhook"):
    latency = None
    if tx_time:
        latency = time.time() - float(tx_time)

    live_price = get_current_price_sol(mint)

    with state_lock:
        pos = state["positions"].get(mint)
        if not pos:
            return
        fallback = pos["avg_price_sol"]

    wallet_price = (
        wallet_sol_received / wallet_token_amount
    ) if wallet_token_amount else None

    exit_price = live_price if live_price else (wallet_price or fallback)
    exit_price = exit_price * (1 - SLIPPAGE_PCT / 100.0)

    close_position(mint, exit_price, sell_ratio, "wallet", latency)


# ------------------------------------------------------------------
# 7. ΕΠΕΞΕΡΓΑΣΙΑ TRANSACTION
# ------------------------------------------------------------------
def get_wallet_token_balance(mint):
    try:
        url = f"https://api.helius.xyz/v0/addresses/{WATCHED_WALLET}/balances"
        res = requests.get(url, params={"api-key": HELIUS_API_KEY}, timeout=15)
        if res.status_code == 200:
            for t in res.json().get("tokens", []):
                if t.get("mint") == mint:
                    decimals = t.get("decimals", 0)
                    return t.get("amount", 0) / (10 ** decimals)
        return 0.0
    except Exception as e:
        print(f"Balance fetch error: {e}")
        return None


def process_transaction(tx, via="webhook"):
    try:
        signature = tx.get("signature")
        if not signature or already_processed(signature):
            return

        tx_time = tx.get("timestamp")
        source = tx.get("source", "")
        tx_type = tx.get("type", "")

        # ΔΙΟΡΘΩΣΗ: δέχεται ΟΛΑ τα DEX, όχι μόνο pump.fun.
        # Αλλιώς χάναμε τις πωλήσεις μετά το bonding σε Raydium.
        is_swap = (source in DEX_SOURCES) or (tx_type == "SWAP")
        if not is_swap:
            return

        token_transfers = tx.get("tokenTransfers", []) or []
        native_transfers = tx.get("nativeTransfers", []) or []

        for tt in token_transfers:
            mint = tt.get("mint")
            amount = tt.get("tokenAmount")
            if not mint or not amount:
                continue
            if mint in IGNORED_MINTS:
                continue

            if tt.get("toUserAccount") == WATCHED_WALLET:
                sol_spent = sum(
                    nt["amount"] for nt in native_transfers
                    if nt.get("fromUserAccount") == WATCHED_WALLET
                ) / 1e9
                paper_buy(mint, amount, sol_spent, signature, tx_time, via)

            elif tt.get("fromUserAccount") == WATCHED_WALLET:
                sol_received = sum(
                    nt["amount"] for nt in native_transfers
                    if nt.get("toUserAccount") == WATCHED_WALLET
                ) / 1e9

                remaining = get_wallet_token_balance(mint)
                if remaining is not None:
                    total_before = amount + remaining
                    ratio = amount / total_before if total_before > 0 else 1.0
                else:
                    ratio = 1.0

                paper_sell(mint, amount, sol_received, signature,
                           tx_time, ratio, via)

    except Exception as e:
        print(f"Tx parse error: {e}")


# ------------------------------------------------------------------
# 8. FLASK ROUTES
# ------------------------------------------------------------------
@app.route("/")
def home():
    return "Pump.fun Paper Copy-Trader is Running!", 200


@app.route("/helius-webhook", methods=["POST"])
def helius_webhook():
    if WEBHOOK_AUTH:
        if request.headers.get("Authorization") != WEBHOOK_AUTH:
            print("Webhook: unauthorized request rejected")
            return jsonify({"error": "unauthorized"}), 401

    try:
        payload = request.get_json(force=True, silent=True)
        if payload is None:
            return jsonify({"error": "no json"}), 400

        txs = payload if isinstance(payload, list) else [payload]
        print(f"Webhook received: {len(txs)} tx(s)")

        Thread(
            target=lambda: [process_transaction(t, via="webhook") for t in txs],
            daemon=True
        ).start()

        return jsonify({"ok": True}), 200
    except Exception as e:
        print(f"Webhook error: {e}")
        return jsonify({"error": str(e)}), 500


# ------------------------------------------------------------------
# 9. POLLING
# ------------------------------------------------------------------
def fetch_new_transactions(until_signature):
    url = f"https://api.helius.xyz/v0/addresses/{WATCHED_WALLET}/transactions"
    params = {"api-key": HELIUS_API_KEY, "limit": 100}
    if until_signature:
        params["until"] = until_signature
    res = requests.get(url, params=params, timeout=15)
    res.raise_for_status()
    return list(reversed(res.json() or []))


def wallet_watcher_loop():
    if not (HELIUS_API_KEY and WATCHED_WALLET):
        print("HELIUS_API_KEY ή WATCHED_WALLET λείπουν.")
        return

    print(f"Safety-net polling active (every {POLL_SECONDS}s)...")

    if state["last_signature"] is None:
        try:
            txs = fetch_new_transactions(None)
            if txs:
                with state_lock:
                    state["last_signature"] = txs[-1]["signature"]
                    save_state()
                for t in txs:
                    already_processed(t["signature"])
                print(f"Baseline set: {txs[-1]['signature'][:16]}...")
        except Exception as e:
            print(f"Baseline fetch error: {e}")

    while True:
        try:
            txs = fetch_new_transactions(state["last_signature"])
            for tx in txs:
                process_transaction(tx, via="polling")
                with state_lock:
                    state["last_signature"] = tx["signature"]
            if txs:
                with state_lock:
                    save_state()
        except Exception as e:
            print(f"Watcher loop error: {e}")
        time.sleep(POLL_SECONDS)


# ------------------------------------------------------------------
# 10. AI ΒΟΗΘΟΣ
# ------------------------------------------------------------------
SYSTEM_PROMPT = """Είσαι ο προσωπικός βοηθός trading του χρήστη.

ΤΟ CONTEXT ΣΟΥ:
Παρακολουθείς ένα Solana wallet που κάνει memecoin trades.
Ο χρήστης τρέχει PAPER trading για να δει αν η αντιγραφή
αυτού του wallet θα ήταν κερδοφόρα.

ΠΩΣ ΑΠΑΝΤΑΣ:
- Σύντομα και πρακτικά. Στα ελληνικά.
- ΠΟΤΕ μη ρωτάς "τι εννοείς" - ξέρεις το context.
- ΠΟΤΕ μην εφευρίσκεις νούμερα. Αν κάτι δεν υπάρχει στα δεδομένα,
  πες "δεν το μετράω αυτό".
- "καθυστέρηση" = δευτ. από τη συναλλαγή του wallet μέχρι να τη δούμε.
  "χρόνος κρατήματος" = πόσο έμεινε ανοιχτή η θέση. Μην τα μπερδεύεις.
- ΣΗΜΑΝΤΙΚΟ: τα trades χωρίζονται σε "wallet" (ακολουθήσαμε τον
  trader) και "manual" (τα έκλεισε ο χρήστης μόνος του). Μόνο τα
  "wallet" δείχνουν αν αξίζει το copy trading. Ανάφερέ το αν
  υπάρχουν manual trades στα δεδομένα.
- ΔΕΝ μπορείς να κλείσεις θέσεις. Πες του να χρησιμοποιήσει /close.
- Λες την αλήθεια για τις ζημιές.
- Δεν προβλέπεις τιμές, δεν δίνεις επενδυτικές συμβουλές."""


def latency_stats():
    with state_lock:
        lats = list(state.get("latencies", []))
    if not lats:
        return None
    s = sorted(lats)
    n = len(s)
    return {"count": n, "avg": sum(s) / n, "median": s[n // 2],
            "best": s[0], "worst": s[-1]}


def build_portfolio_context():
    with state_lock:
        closed = list(state["closed_trades"])
        positions = dict(state["positions"])
        balance = state["balance_sol"]
        fees = state.get("total_fees_sol", 0.0)

    by_wallet = [t for t in closed if t.get("closed_by", "wallet") == "wallet"]
    by_manual = [t for t in closed if t.get("closed_by") == "manual"]

    def stats(trades):
        if not trades:
            return "καμία"
        pnl = sum(t["pnl_sol"] for t in trades)
        wins = sum(1 for t in trades if t["pnl_sol"] > 0)
        return (f"{len(trades)} trades, P&L {pnl:+.4f} SOL, "
                f"win rate {wins/len(trades)*100:.0f}%")

    lines = [
        f"Virtual balance: {balance:.3f} SOL (από {STARTING_BALANCE_SOL:.3f})",
        f"Fees: {fees:.4f} SOL",
        f"Ανοιχτές θέσεις: {len(positions)}",
        f"Slippage model: {SLIPPAGE_PCT}%",
        "",
        f"ΚΛΕΙΣΤΑ ΑΠΟ ΤΟΝ TRADER (αυτά μετράνε για copy trading):",
        f"  {stats(by_wallet)}",
        f"ΚΛΕΙΣΤΑ ΧΕΙΡΟΚΙΝΗΤΑ ΑΠΟ ΤΟΝ ΧΡΗΣΤΗ:",
        f"  {stats(by_manual)}",
    ]

    ls = latency_stats()
    if ls:
        lines.append(
            f"\nΚΑΘΥΣΤΕΡΗΣΗ ΑΝΙΧΝΕΥΣΗΣ: {ls['count']} μετρήσεις | "
            f"μ.ο. {ls['avg']:.1f}s | διάμεσος {ls['median']:.1f}s | "
            f"καλύτερη {ls['best']:.1f}s | χειρότερη {ls['worst']:.1f}s"
        )
    else:
        lines.append("\nΚΑΘΥΣΤΕΡΗΣΗ: καμία μέτρηση ακόμα.")

    if closed:
        lines.append("\nΤελευταία trades:")
        for t in closed[-10:]:
            lat = t.get("entry_latency")
            lat_s = f", lag {lat:.1f}s" if lat else ""
            by = t.get("closed_by", "wallet")
            lines.append(
                f"  {short_mint(t['mint'])}: {t['pnl_sol']:+.4f} SOL "
                f"({t['pnl_pct']:+.1f}%), {t.get('held_minutes', 0):.1f}min"
                f"{lat_s} [{by}]"
            )

    if positions:
        lines.append("\nΑνοιχτές θέσεις:")
        for mint, pos in positions.items():
            lines.append(
                f"  {short_mint(mint)}: επενδεδυμένα "
                f"{pos.get('invested_sol', 0):.3f} SOL σε "
                f"{pos.get('buys', 1)} αγορές"
            )

    return "\n".join(lines)


def ask_ai(user_text):
    if not ai_client:
        return "⚠️ Το GEMINI_API_KEY δεν έχει ρυθμιστεί."
    try:
        prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"=== ΔΕΔΟΜΕΝΑ PAPER ACCOUNT ===\n"
            f"{build_portfolio_context()}\n"
            f"=== ΤΕΛΟΣ ΔΕΔΟΜΕΝΩΝ ===\n\n"
            f"Ερώτηση: {user_text}"
        )
        response = ai_client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt
        )
        return response.text
    except Exception as e:
        return f"⚠️ Σφάλμα AI: {e}"


# ------------------------------------------------------------------
# 11. TELEGRAM COMMANDS
# ------------------------------------------------------------------
def do_manual_close(arg):
    """arg: κενό = όλες οι θέσεις, αλλιώς τα πρώτα γράμματα του mint."""
    with state_lock:
        mints = list(state["positions"].keys())

    if not mints:
        send_telegram_message("Καμία ανοιχτή θέση.")
        return

    targets = mints
    if arg:
        arg = arg.strip().lower()
        targets = [m for m in mints if m.lower().startswith(arg)]
        if not targets:
            send_telegram_message(
                f"Δεν βρέθηκε θέση που να ξεκινάει με <code>{arg}</code>.\n"
                f"Ανοιχτές: {', '.join(short_mint(m) for m in mints)}"
            )
            return

    for mint in targets:
        price = get_current_price_sol(mint)
        if price is None:
            send_telegram_message(
                f"⚠️ Δεν βρήκα τρέχουσα τιμή για {short_mint(mint)}. "
                f"Δεν έκλεισα τη θέση."
            )
            continue
        exit_price = price * (1 - SLIPPAGE_PCT / 100.0)
        close_position(mint, exit_price, 1.0, "manual")


def handle_command(text):
    raw = text.strip()
    cmd = raw.lower()

    if cmd == "/start":
        send_telegram_message(
            "🤖 <b>Paper Copy-Trader Online</b>\n\n"
            f"Wallet: <code>{short_mint(WATCHED_WALLET)}</code>\n"
            f"Balance: {state['balance_sol']:.3f} SOL\n"
            f"Slippage: {SLIPPAGE_PCT}%\n\n"
            "/status /positions /pnl /latency /close /analyze /reset\n\n"
            "<i>/close κλείνει όλες τις θέσεις.\n"
            "/close 28fH κλείνει συγκεκριμένη.</i>"
        )

    elif cmd.startswith("/close"):
        arg = raw[6:].strip()
        do_manual_close(arg)

    elif cmd == "/latency":
        ls = latency_stats()
        if not ls:
            send_telegram_message("Καμία μέτρηση ακόμα.")
            return
        send_telegram_message(
            f"⏱ <b>Καθυστέρηση ανίχνευσης</b>\n"
            f"Μετρήσεις: {ls['count']}\n"
            f"Μέσος όρος: {ls['avg']:.1f}s\n"
            f"Διάμεσος: {ls['median']:.1f}s\n"
            f"Καλύτερη: {ls['best']:.1f}s\n"
            f"Χειρότερη: {ls['worst']:.1f}s"
        )

    elif cmd == "/status":
        with state_lock:
            send_telegram_message(
                f"📊 <b>Status</b>\n"
                f"Wallet: <code>{short_mint(WATCHED_WALLET)}</code>\n"
                f"Balance: {state['balance_sol']:.3f} SOL\n"
                f"Ανοιχτές: {len(state['positions'])} | "
                f"Κλειστές: {len(state['closed_trades'])}\n"
                f"Fees: {state.get('total_fees_sol', 0):.4f} SOL"
            )

    elif cmd == "/positions":
        with state_lock:
            positions = dict(state["positions"])
        if not positions:
            send_telegram_message("Καμία ανοιχτή θέση.")
            return
        lines = ["📈 <b>Ανοιχτές Θέσεις</b>\n"]
        total_unreal = 0.0
        for mint, pos in positions.items():
            cur = get_current_price_sol(mint)
            line = (f"• <code>{short_mint(mint)}</code>\n"
                    f"  {pos.get('invested_sol', 0):.3f} SOL σε "
                    f"{pos.get('buys', 1)} αγορές @ {pos['avg_price_sol']:.8f}")
            if cur:
                unreal = (cur - pos["avg_price_sol"]) * pos["amount"]
                total_unreal += unreal
                pct = (cur / pos["avg_price_sol"] - 1) * 100 if pos["avg_price_sol"] else 0
                line += f"\n  Τώρα: {cur:.8f} | {unreal:+.4f} SOL ({pct:+.1f}%)"
            lines.append(line)
        lines.append(f"\n<b>Σύνολο unrealized: {total_unreal:+.4f} SOL</b>")
        send_telegram_message("\n".join(lines))

    elif cmd == "/pnl":
        with state_lock:
            closed = list(state["closed_trades"])
            positions = dict(state["positions"])
            balance = state["balance_sol"]
            fees = state.get("total_fees_sol", 0.0)

        by_wallet = [t for t in closed if t.get("closed_by", "wallet") == "wallet"]
        by_manual = [t for t in closed if t.get("closed_by") == "manual"]

        w_pnl = sum(t["pnl_sol"] for t in by_wallet)
        m_pnl = sum(t["pnl_sol"] for t in by_manual)
        w_wins = sum(1 for t in by_wallet if t["pnl_sol"] > 0)
        m_wins = sum(1 for t in by_manual if t["pnl_sol"] > 0)

        unrealized = 0.0
        for mint, pos in positions.items():
            cur = get_current_price_sol(mint)
            if cur:
                unrealized += (cur - pos["avg_price_sol"]) * pos["amount"]

        msg = f"💰 <b>P&L Summary</b>\n\n"
        msg += f"📋 <b>Ακολουθώντας τον trader</b>\n"
        if by_wallet:
            msg += (f"  {w_pnl:+.4f} SOL | {len(by_wallet)} trades | "
                    f"win rate {w_wins/len(by_wallet)*100:.0f}%\n")
        else:
            msg += "  καμία κλειστή θέση\n"

        if by_manual:
            msg += f"\n✋ <b>Χειροκίνητα κλεισίματα</b>\n"
            msg += (f"  {m_pnl:+.4f} SOL | {len(by_manual)} trades | "
                    f"win rate {m_wins/len(by_manual)*100:.0f}%\n")

        msg += (f"\nUnrealized ({len(positions)} ανοιχτές): {unrealized:+.4f} SOL\n"
                f"Fees: -{fees:.4f} SOL\n"
                f"<b>Balance: {balance:.3f} (από {STARTING_BALANCE_SOL:.3f})</b>")
        send_telegram_message(msg)

    elif cmd == "/analyze":
        send_telegram_message("🧠 Αναλύω...")
        send_telegram_message(ask_ai(
            "Ανάλυσε την απόδοση. Ξεχώρισε τα trades που έκλεισαν "
            "ακολουθώντας τον trader από αυτά που έκλεισα εγώ. "
            "Πόσο με κοστίζει η καθυστέρηση; Αξίζει με πραγματικά λεφτά;"
        ))

    elif cmd == "/reset":
        with state_lock:
            state["balance_sol"] = STARTING_BALANCE_SOL
            state["positions"] = {}
            state["closed_trades"] = []
            state["total_fees_sol"] = 0.0
            state["latencies"] = []
            save_state()
        send_telegram_message(f"🔄 Reset. Balance: {STARTING_BALANCE_SOL:.3f} SOL")

    else:
        send_telegram_message(ask_ai(text))


def telegram_polling_loop():
    clear_telegram_webhooks()
    last_update_id = 0
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    print("Telegram Listener loop active...")

    while True:
        try:
            params = {"offset": last_update_id + 1, "timeout": 20}
            response = requests.get(url, params=params, timeout=25)
            if response.status_code == 200:
                for update in response.json().get("result", []):
                    last_update_id = update["update_id"]
                    message = update.get("message", {})
                    text = message.get("text", "")
                    sender_id = str(message.get("chat", {}).get("id", ""))
                    if TELEGRAM_CHAT_ID and sender_id == str(TELEGRAM_CHAT_ID) and text:
                        handle_command(text)
            elif response.status_code == 409:
                print("Telegram 409: άλλο instance τρέχει. Περιμένω...")
                time.sleep(10)
        except Exception as e:
            print(f"Telegram listener error: {e}")
        time.sleep(2)


# ------------------------------------------------------------------
# 12. MAIN
# ------------------------------------------------------------------
if __name__ == "__main__":
    Thread(target=telegram_polling_loop, daemon=True).start()
    Thread(target=wallet_watcher_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
