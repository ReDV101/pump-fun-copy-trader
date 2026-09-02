import os
import time
import json
import requests
from flask import Flask
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

STARTING_BALANCE_SOL = float(os.getenv("STARTING_BALANCE_SOL", "10"))
PAPER_TRADE_SOL = float(os.getenv("PAPER_TRADE_SOL", "0.5"))

# --- ΝΕΟ: ρεαλιστικό κόστος συναλλαγής ---
# Slippage = πόσο χειρότερη τιμή παίρνεις επειδή μπαίνεις αργότερα.
# 8% είναι συντηρητικό για pump.fun. Ανέβασέ το αν θες αυστηρότερο τεστ.
SLIPPAGE_PCT = float(os.getenv("SLIPPAGE_PCT", "8"))
# Fees ανά συναλλαγή σε SOL (priority fee + Jito tip + pump.fun fee)
FEE_SOL = float(os.getenv("FEE_SOL", "0.003"))

# --- ΝΕΟ: persistent path για Railway Volume ---
# Στο Railway: Settings -> Volumes -> Mount path /data
# Μετά βάλε Variable: STATE_PATH = /data/paper_state.json
STATE_FILE = os.getenv("STATE_PATH", "paper_state.json")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "10"))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

state_lock = Lock()


# ------------------------------------------------------------------
# 2. PAPER TRADING STATE
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
        "positions": {},        # mint -> {"amount", "avg_price_sol", "opened_at"}
        "closed_trades": [],
        "last_signature": None,
        "total_fees_sol": 0.0,
    }


def save_state():
    """ΠΡΟΣΟΧΗ: καλείται ΠΑΝΤΑ με το state_lock κρατημένο."""
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)   # atomic write, δεν χαλάει το αρχείο σε crash
    except Exception as e:
        print(f"State save error: {e}")


state = load_state()


# ------------------------------------------------------------------
# 3. GEMINI AI CLIENT
# ------------------------------------------------------------------
ai_client = None
if GEMINI_API_KEY:
    try:
        ai_client = genai.Client(api_key=GEMINI_API_KEY)
        print("Gemini AI Client initialized successfully!")
    except Exception as e:
        print(f"Gemini Init Error: {e}")


# ------------------------------------------------------------------
# 4. FLASK health check
# ------------------------------------------------------------------
@app.route("/")
def home():
    return "Pump.fun Paper Copy-Trader is Running!", 200


# ------------------------------------------------------------------
# 5. TELEGRAM HELPERS
# ------------------------------------------------------------------
def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[No Telegram configured] {text}")
        return
    # Telegram κόβει στα 4096 χαρακτήρες
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
        res = requests.get(url, timeout=10)
        print("Cleared Telegram Webhooks." if res.status_code == 200
              else f"Webhook clear status: {res.status_code}")
    except Exception as e:
        print(f"Webhook reset warning: {e}")


def short_mint(mint):
    return f"{mint[:4]}...{mint[-4:]}" if mint and len(mint) > 8 else mint


# ------------------------------------------------------------------
# 6. ΤΙΜΗ TOKEN (DexScreener, δωρεάν)
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
# 7. PAPER TRADING LOGIC  (ΔΙΟΡΘΩΜΕΝΟ)
# ------------------------------------------------------------------
def paper_buy(mint, wallet_token_amount, wallet_sol_spent, signature):
    """
    ΔΙΟΡΘΩΣΗ #1: δεν χρησιμοποιούμε την τιμή του wallet.
    Παίρνουμε την ΤΩΡΙΝΗ τιμή αγοράς (τη στιγμή που το εντοπίσαμε εμείς),
    και προσθέτουμε slippage + fees. Αυτό είναι το ρεαλιστικό σου entry.
    """
    if not wallet_token_amount or not wallet_sol_spent:
        return

    wallet_price = wallet_sol_spent / wallet_token_amount

    # Η πραγματική τιμή τη στιγμή που ΕΜΕΙΣ θα προλαβαίναμε να μπούμε
    live_price = get_current_price_sol(mint)
    if live_price is None:
        # fallback: τιμή wallet + slippage
        live_price = wallet_price
    entry_price = live_price * (1 + SLIPPAGE_PCT / 100.0)

    if entry_price <= 0:
        return

    with state_lock:
        total_cost = PAPER_TRADE_SOL + FEE_SOL
        if state["balance_sol"] < total_cost:
            send_telegram_message(
                f"⚠️ Ανεπαρκές virtual balance για copy-buy σε {short_mint(mint)}"
            )
            return

        tokens_bought = PAPER_TRADE_SOL / entry_price

        pos = state["positions"].get(
            mint, {"amount": 0.0, "avg_price_sol": 0.0, "opened_at": time.time()}
        )
        cost_before = pos["amount"] * pos["avg_price_sol"]
        new_amount = pos["amount"] + tokens_bought
        new_avg_price = (cost_before + PAPER_TRADE_SOL) / new_amount

        state["positions"][mint] = {
            "amount": new_amount,
            "avg_price_sol": new_avg_price,
            "opened_at": pos.get("opened_at", time.time()),
        }
        state["balance_sol"] -= total_cost
        state["total_fees_sol"] += FEE_SOL
        save_state()
        balance_now = state["balance_sol"]

    # Πόσο χειρότερα μπήκαμε από το wallet - αυτό είναι το κρίσιμο νούμερο
    slip_vs_wallet = (entry_price / wallet_price - 1) * 100 if wallet_price else 0

    send_telegram_message(
        f"🟢 <b>PAPER BUY</b>\n"
        f"Token: <code>{short_mint(mint)}</code>\n"
        f"Ποσό: {PAPER_TRADE_SOL:.3f} SOL (+{FEE_SOL:.4f} fee)\n"
        f"Τιμή wallet: {wallet_price:.8f}\n"
        f"Δικό μας entry: {entry_price:.8f} ({slip_vs_wallet:+.1f}% χειρότερα)\n"
        f"Balance: {balance_now:.3f} SOL\n"
        f"Tx: <code>{signature}</code>"
    )


def paper_sell(mint, wallet_token_amount, wallet_sol_received, signature):
    """
    ΔΙΟΡΘΩΣΗ #2: αναλογική πώληση.
    Αν το wallet πούλησε το 30% του bag του, πουλάμε το 30% του δικού μας.
    """
    live_price = get_current_price_sol(mint)

    with state_lock:
        pos = state["positions"].get(mint)
        if not pos or pos["amount"] <= 0:
            return

        wallet_price = (
            wallet_sol_received / wallet_token_amount
        ) if wallet_token_amount else None

        exit_price = live_price if live_price else (wallet_price or pos["avg_price_sol"])
        exit_price = exit_price * (1 - SLIPPAGE_PCT / 100.0)  # slippage και στην έξοδο

        # --- αναλογία πώλησης ---
        # Χρειαζόμαστε πόσα tokens κρατούσε το wallet ΠΡΙΝ πουλήσει.
        # Αν δεν το ξέρουμε, θεωρούμε πλήρη έξοδο (συντηρητικό).
        sell_ratio = state.get("_pending_ratio", {}).pop(mint, 1.0) \
            if isinstance(state.get("_pending_ratio"), dict) else 1.0
        sell_ratio = max(0.0, min(1.0, sell_ratio))

        tokens_sold = pos["amount"] * sell_ratio
        if tokens_sold <= 0:
            return

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
            "signature": signature,
            "timestamp": time.time(),
        })

        remaining = pos["amount"] - tokens_sold
        if remaining > 1e-9:
            state["positions"][mint]["amount"] = remaining
        else:
            del state["positions"][mint]

        save_state()
        balance_now = state["balance_sol"]

    emoji = "✅" if pnl >= 0 else "❌"
    send_telegram_message(
        f"🔴 <b>PAPER SELL</b> {emoji}\n"
        f"Token: <code>{short_mint(mint)}</code>\n"
        f"Πουλήθηκε: {sell_ratio*100:.0f}% της θέσης\n"
        f"P&L: {pnl:+.4f} SOL ({pnl_pct:+.1f}%)\n"
        f"Κρατήθηκε: {held_min:.0f} λεπτά\n"
        f"Balance: {balance_now:.3f} SOL\n"
        f"Tx: <code>{signature}</code>"
    )


# ------------------------------------------------------------------
# 8. HELIUS WALLET WATCHER
# ------------------------------------------------------------------
def fetch_new_transactions(until_signature):
    url = f"https://api.helius.xyz/v0/addresses/{WATCHED_WALLET}/transactions"
    params = {"api-key": HELIUS_API_KEY, "limit": 100}
    if until_signature:
        params["until"] = until_signature
    res = requests.get(url, params=params, timeout=15)
    res.raise_for_status()
    txs = res.json() or []
    return list(reversed(txs))  # παλιότερο -> νεότερο


def get_wallet_token_balance(mint):
    """Πόσα tokens κρατάει ΤΩΡΑ το watched wallet - για να βρούμε το sell ratio."""
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


def process_transaction(tx):
    try:
        signature = tx.get("signature")
        source = tx.get("source", "")
        involves_pumpfun = (
            source in ("PUMP_FUN", "PUMP_AMM")
            or PUMP_FUN_PROGRAM_ID in json.dumps(tx.get("instructions", []))
        )
        if not involves_pumpfun:
            return

        token_transfers = tx.get("tokenTransfers", []) or []
        native_transfers = tx.get("nativeTransfers", []) or []

        for tt in token_transfers:
            mint = tt.get("mint")
            amount = tt.get("tokenAmount")
            if not mint or not amount:
                continue

            if tt.get("toUserAccount") == WATCHED_WALLET:
                sol_spent = sum(
                    nt["amount"] for nt in native_transfers
                    if nt.get("fromUserAccount") == WATCHED_WALLET
                ) / 1e9
                paper_buy(mint, amount, sol_spent, signature)

            elif tt.get("fromUserAccount") == WATCHED_WALLET:
                sol_received = sum(
                    nt["amount"] for nt in native_transfers
                    if nt.get("toUserAccount") == WATCHED_WALLET
                ) / 1e9

                # Υπολογισμός sell ratio: πουλημένα / (πουλημένα + όσα έμειναν)
                remaining = get_wallet_token_balance(mint)
                if remaining is not None:
                    total_before = amount + remaining
                    ratio = amount / total_before if total_before > 0 else 1.0
                else:
                    ratio = 1.0

                with state_lock:
                    if not isinstance(state.get("_pending_ratio"), dict):
                        state["_pending_ratio"] = {}
                    state["_pending_ratio"][mint] = ratio

                paper_sell(mint, amount, sol_received, signature)

    except Exception as e:
        print(f"Tx parse error: {e}")


def wallet_watcher_loop():
    if not (HELIUS_API_KEY and WATCHED_WALLET):
        print("HELIUS_API_KEY ή WATCHED_WALLET λείπουν - watcher ανενεργός.")
        return

    print("Wallet watcher loop active...")

    if state["last_signature"] is None:
        try:
            txs = fetch_new_transactions(None)
            if txs:
                with state_lock:
                    state["last_signature"] = txs[-1]["signature"]
                    save_state()
                print(f"Baseline set: {txs[-1]['signature'][:16]}...")
        except Exception as e:
            print(f"Baseline fetch error: {e}")

    while True:
        try:
            txs = fetch_new_transactions(state["last_signature"])
            for tx in txs:
                process_transaction(tx)
                with state_lock:
                    state["last_signature"] = tx["signature"]
            if txs:
                with state_lock:
                    save_state()
        except Exception as e:
            print(f"Watcher loop error: {e}")
        time.sleep(POLL_SECONDS)


# ------------------------------------------------------------------
# 9. AI ΒΟΗΘΟΣ - με context (ΔΙΟΡΘΩΣΗ #4)
# ------------------------------------------------------------------
SYSTEM_PROMPT = """Είσαι ο προσωπικός βοηθός trading του χρήστη.

ΤΟ CONTEXT ΣΟΥ:
Παρακολουθείς ένα συγκεκριμένο Solana wallet που κάνει pump.fun trades.
Ο χρήστης τρέχει PAPER trading (προσομοίωση, χωρίς πραγματικά λεφτά)
για να δει αν η αντιγραφή αυτού του wallet θα ήταν κερδοφόρα.

ΠΩΣ ΑΠΑΝΤΑΣ:
- Σύντομα και πρακτικά. Στα ελληνικά.
- ΠΟΤΕ μη ρωτάς "τι εννοείς" ή "τι είδους wallet" - ξέρεις ήδη το context.
- Όταν σου δίνονται δεδομένα portfolio, ανάλυσέ τα με νούμερα.
- Λες την αλήθεια για τις ζημιές. Μη γλυκαίνεις τα κακά αποτελέσματα.
- Δεν προβλέπεις τιμές και δεν δίνεις επενδυτικές συμβουλές.
  Αναλύεις τι ΕΓΙΝΕ, όχι τι θα γίνει."""


def build_portfolio_context():
    """Δίνει στο AI την πραγματική εικόνα του paper account."""
    with state_lock:
        realized = sum(t["pnl_sol"] for t in state["closed_trades"])
        closed = list(state["closed_trades"])
        positions = dict(state["positions"])
        balance = state["balance_sol"]
        fees = state.get("total_fees_sol", 0.0)

    wins = sum(1 for t in closed if t["pnl_sol"] > 0)
    total = len(closed)
    winrate = (wins / total * 100) if total else 0
    avg_hold = (sum(t.get("held_minutes", 0) for t in closed) / total) if total else 0

    lines = [
        f"Virtual balance: {balance:.3f} SOL (ξεκίνησε από {STARTING_BALANCE_SOL:.3f})",
        f"Πραγματοποιημένο P&L: {realized:+.4f} SOL",
        f"Fees πληρωμένα: {fees:.4f} SOL",
        f"Κλειστές θέσεις: {total} | Win rate: {winrate:.0f}%",
        f"Μέσος χρόνος κρατήματος: {avg_hold:.0f} λεπτά",
        f"Slippage που μοντελοποιείται: {SLIPPAGE_PCT}%",
        f"Ανοιχτές θέσεις: {len(positions)}",
    ]

    if closed:
        recent = closed[-10:]
        lines.append("\nΤελευταία trades:")
        for t in recent:
            lines.append(
                f"  {short_mint(t['mint'])}: {t['pnl_sol']:+.4f} SOL "
                f"({t['pnl_pct']:+.1f}%), {t.get('held_minutes', 0):.0f}min"
            )

    return "\n".join(lines)


def ask_ai(user_text):
    if not ai_client:
        return "⚠️ Το GEMINI_API_KEY δεν έχει ρυθμιστεί."
    try:
        prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"=== ΤΡΕΧΟΥΣΑ ΚΑΤΑΣΤΑΣΗ PAPER ACCOUNT ===\n"
            f"{build_portfolio_context()}\n"
            f"=== ΤΕΛΟΣ ΔΕΔΟΜΕΝΩΝ ===\n\n"
            f"Ερώτηση χρήστη: {user_text}"
        )
        response = ai_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        return response.text
    except Exception as e:
        return f"⚠️ Σφάλμα AI: {e}"


# ------------------------------------------------------------------
# 10. TELEGRAM COMMANDS
# ------------------------------------------------------------------
def handle_command(text):
    cmd = text.strip().lower()

    if cmd == "/start":
        send_telegram_message(
            "🤖 <b>Pump.fun Paper Copy-Trader Online!</b>\n\n"
            f"Wallet: <code>{short_mint(WATCHED_WALLET)}</code>\n"
            f"Virtual balance: {state['balance_sol']:.3f} SOL\n"
            f"Slippage model: {SLIPPAGE_PCT}%\n\n"
            "Εντολές: /status /positions /pnl /analyze /reset\n"
            "Ή γράψε ελεύθερα για AI ανάλυση."
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
        for mint, pos in positions.items():
            cur = get_current_price_sol(mint)
            line = (f"• {short_mint(mint)}: {pos['amount']:.2f} @ "
                    f"{pos['avg_price_sol']:.8f}")
            if cur:
                unreal = (cur - pos["avg_price_sol"]) * pos["amount"]
                pct = (cur / pos["avg_price_sol"] - 1) * 100 if pos["avg_price_sol"] else 0
                line += f"\n  Τώρα: {cur:.8f} | {unreal:+.4f} SOL ({pct:+.1f}%)"
            lines.append(line)
        send_telegram_message("\n".join(lines))

    elif cmd == "/pnl":
        with state_lock:
            closed = list(state["closed_trades"])
            balance = state["balance_sol"]
            fees = state.get("total_fees_sol", 0.0)
        realized = sum(t["pnl_sol"] for t in closed)
        wins = sum(1 for t in closed if t["pnl_sol"] > 0)
        total = len(closed)
        winrate = (wins / total * 100) if total else 0
        best = max((t["pnl_sol"] for t in closed), default=0)
        worst = min((t["pnl_sol"] for t in closed), default=0)
        send_telegram_message(
            f"💰 <b>P&L Summary</b>\n"
            f"Realized: {realized:+.4f} SOL\n"
            f"Fees: -{fees:.4f} SOL\n"
            f"Κλειστές: {total} | Win rate: {winrate:.0f}%\n"
            f"Καλύτερο: {best:+.4f} | Χειρότερο: {worst:+.4f}\n"
            f"Balance: {balance:.3f} (από {STARTING_BALANCE_SOL:.3f})"
        )

    elif cmd == "/analyze":
        send_telegram_message("🧠 Αναλύω...")
        send_telegram_message(ask_ai(
            "Ανάλυσε την απόδοση του paper account μου μέχρι τώρα. "
            "Αξίζει να αντιγράφω αυτό το wallet με πραγματικά λεφτά; "
            "Δώσε μου συγκεκριμένα νούμερα και τι σε ανησυχεί."
        ))

    elif cmd == "/reset":
        with state_lock:
            state["balance_sol"] = STARTING_BALANCE_SOL
            state["positions"] = {}
            state["closed_trades"] = []
            state["total_fees_sol"] = 0.0
            save_state()
        send_telegram_message(
            f"🔄 Reset. Balance: {STARTING_BALANCE_SOL:.3f} SOL"
        )

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
            else:
                print(f"Telegram API Status: {response.status_code}")
        except Exception as e:
            print(f"Telegram listener error: {e}")
        time.sleep(2)


# ------------------------------------------------------------------
# 11. MAIN
# ------------------------------------------------------------------
if __name__ == "__main__":
    Thread(target=telegram_polling_loop, daemon=True).start()
    Thread(target=wallet_watcher_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
