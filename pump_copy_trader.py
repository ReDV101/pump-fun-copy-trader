requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook?drop_pending_updates=True", timeout=10)
        print("Cleared Telegram Webhooks successfully.")
    except Exception as e:
        print(f"Webhook reset warning: {e}")

    last_update_id = 0
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    
    print("Telegram Listener loop active...")
    while True:
        try:
            params = {"offset": last_update_id + 1, "timeout": 20}
            response = requests.get(url, params=params, timeout=25)
            
            if response.status_code == 200:
                data = response.json()
                for update in data.get("result", []):
                    last_update_id = update["update_id"]
                    message = update.get("message", {})
                    text = message.get("text", "")
                    sender_id = str(message.get("chat", {}).get("id", ""))

                    # Απάντηση ΜΟΝΟ στον δικό σου λογαριασμό Telegram για ασφάλεια
                    if sender_id == str(TELEGRAM_CHAT_ID) and text:
                        if text == "/start":
                            send_telegram_message("🤖 <b>AI Assistant Online!</b>\nΕίμαι έτοιμος! Στείλε μου οποιαδήποτε ερώτηση.")
                        elif ai_client:
                            try:
                                print(f"Processing AI Query: {text}")
                                response_ai = ai_client.models.generate_content(
                                    model='gemini-2.5-flash',
                                    contents=text,
                                )
                                send_telegram_message(response_ai.text)
                            except Exception as ai_err:
                                print(f"AI Generation Error: {ai_err}")
                                send_telegram_message(f"⚠️ Σφάλμα AI: {ai_err}")
                        else:
                            send_telegram_message("⚠️ Το GEMINI_API_KEY δεν βρέθηκε στο Render!")
            else:
                print(f"Telegram API Status Code: {response.status_code}")
        except Exception as e:
            print(f"Telegram listener error: {e}")
        
        time.sleep(1)

# -------------------------------------------------------------
# 2. PUMP.FUN TRADES MONITOR THREAD (Free HTTP Polling)
# -------------------------------------------------------------
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
    send_telegram_message(f"🚀 <b>System Active:</b> AI Assistant & Pump.fun Watcher Ready!\n<code>{WATCHED_WALLET}</code>")
    last_signature = None
    last_signature = get_latest_transactions(last_signature)
    
    while True:
        try:
            last_signature = get_latest_transactions(last_signature)
        except Exception as e:
            print(f"Error in polling loop: {e}")
        time.sleep(3)

# -------------------------------------------------------------
# MAIN SERVER LAUNCH
# -------------------------------------------------------------
def run_flask():
    # Παίρνουμε το PORT δυναμικά από το Render (default 10000)
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    # 1. Thread για Blockchain Monitoring
    t_pump = Thread(target=poll_blockchain)
    t_pump.daemon = True
    t_pump.start()
    
    # 2. Thread για Telegram AI Assistant
    t_ai = Thread(target=handle_telegram_updates)
    t_ai.daemon = True
    t_ai.start()
    
    # 3. Web Server
    run_flask()
was
        

