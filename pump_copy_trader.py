import os
import time
import requests
from flask import Flask
from threading import Thread
from google import genai

app = Flask(__name__)

# Μεταβλητές Περιβάλλοντος
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
WATCHED_WALLET = os.getenv("WATCHED_WALLET")
TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Αρχικοποίηση Gemini Client
ai_client = None
if GEMINI_API_KEY:
    ai_client = genai.Client(api_key=GEMINI_API_KEY)

@app.route('/')
def home():
    return "Pump.fun Copy Trader & AI Assistant is Running!", 200

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Error sending Telegram message: {e}")

# 1. AI ASSISTANT (Telegram Messages)
def handle_telegram_updates():
    """Ακούει τα μηνύματα που στέλνεις στο Bot και απαντάει με AI"""
    last_update_id = 0
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    
    while True:
        try:
            response = requests.get(url, params={"offset": last_update_id + 1, "timeout": 20}, timeout=25)
            if response.status_code == 200:
                data = response.json()
                for update in data.get("result", []):
                    last_update_id = update["update_id"]
                    message = update.get("message", {})
                    text = message.get("text", "")
                    sender_id = str(message.get("chat", {}).get("id", ""))

                    # Απαντάει μόνο σε εσένα (ασφάλεια)
                    if sender_id == str(TELEGRAM_CHAT_ID) and text:
                        if text.startswith("/start"):
                            send_telegram_message("Γεια σου! Είμαι ο προσωπικός σου AI βοηθός και ταυτόχρονα παρακολουθώ το Pump.fun wallet σου!")
                        elif ai_client:
                            # Παραγωγή απάντησης από το Gemini AI
                            try:
                                ai_response = ai_client.models.generate_content(
                                    model='gemini-2.5-flash',
                                    contents=text,
                                )
                                send_telegram_message(ai_response.text)
                            except Exception as ai_err:
                                send_telegram_message(f"Σφάλμα AI: {ai_err}")
                        else:
                            send_telegram_message("Το GEMINI_API_KEY δεν έχει οριστεί στο Render.")
        except Exception as e:
            print(f"Telegram listener error: {e}")
        time.sleep(1)

# 2. PUMP.FUN MONITOR (HTTP Polling)
def get_latest_transactions(last_signature):
    url = f"https://api.helius.xyz/v0/addresses/{WATCHED_WALLET}/transactions?api-key={HELIUS_API_KEY}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            txs = response.json()
            if not txs:
                return last_signature
            
            if last_signature is None:
                return txs[0]['signature']
            
            new_txs = []
            for tx in txs:
                if tx['signature'] == last_signature:
                    break
                new_txs.append(tx)
            
            for tx in reversed(new_txs):
                sig = tx.get('signature', '')
                instructions = tx.get('instructions', [])
                is_pump = any(inst.get('programId') == PUMP_FUN_PROGRAM_ID for inst in instructions)
                
                if is_pump:
                    msg = (
                        f"🚨 <b>Νέο Trade στο Pump.fun!</b>\n\n"
                        f"<b>Wallet:</b> <code>{WATCHED_WALLET}</code>\n"
                        f"<b>Tx:</b> <a href='https://solscan.io/tx/{sig}'>{sig[:8]}...{sig[-8:]}</a>"
                    )
                    send_telegram_message(msg)
            
            return txs[0]['signature']
    except Exception as e:
        print(f"Polling error: {e}")
    return last_signature

def poll_blockchain():
    print("Starting Free Polling Service...")
    send_telegram_message(f"🚀 <b>Bot Active:</b> AI Assistant & Pump.fun Monitoring Enabled!\n<code>{WATCHED_WALLET}</code>")
    last_signature = None
    last_signature = get_latest_transactions(last_signature)
    
    while True:
        try:
            last_signature = get_latest_transactions(last_signature)
        except Exception as e:
            print(f"Error in loop: {e}")
        time.sleep(3)

def run_flask():
    app.run(host='0.0.0.0', port=10000)

if __name__ == "__main__":
    # 1. Thread για το Pump.fun Monitoring
    t_pump = Thread(target=poll_blockchain)
    t_pump.daemon = True
    t_pump.start()
    
    # 2. Thread για τον AI Assistant στο Telegram
    t_ai = Thread(target=handle_telegram_updates)
    t_ai.daemon = True
    t_ai.start()
    
    # 3. Web Server για το Render
    run_flask()
