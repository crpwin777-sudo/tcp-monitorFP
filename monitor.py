import socket
import json
import os
import datetime
import requests

STATUS_FILE = "statuses.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, json=payload)
        print("TG Response:", r.text)
    except Exception as e:
        print("Failed to send telegram:", e)

def check_port(host, port, timeout=3):
    """Check if TCP port open"""
    try:
        s = socket.socket()
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return True
    except:
        return False

def load_statuses():
    if not os.path.exists(STATUS_FILE):
        return {}
    try:
        with open(STATUS_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_statuses(statuses):
    with open(STATUS_FILE, "w") as f:
        json.dump(statuses, f, indent=2)

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

# ---------------------------
#  DEVICES TO MONITOR
# ---------------------------
#targets = [
#   {"name": "DPC 6 LaJefa", "host": "vanesa-host.ddns.net", "port": 3389},
#   {"name": "DPC 5 AdminPC", "host": "example2.ddns.net", "port": 3389},
#   {"name": "DPC 4 Backup", "host": "example3.ddns.net", "port": 3389}
#]
# Добавь свои ПК по шаблону ↑↑↑

# ---------------------------
#  MAIN
# ---------------------------

statuses = load_statuses()

for t in targets:
    key = f"{t['host']}:{t['port']}"
    prev = statuses.get(key, {}).get("combined", "unknown")

    is_online = check_port(t["host"], t["port"])
    combined = "online" if is_online else "offline"

    record = statuses.get(key, {
        "name": t["name"],
        "host": t["host"],
        "port": t["port"],
        "combined": combined,
        "consec_fails": 0,
        "consec_success": 0
    })

    # Update counters
    if combined == "online":
        record["consec_success"] += 1
        record["consec_fails"] = 0
        if "offline_since" in record:
            del record["offline_since"]
    else:
        record["consec_fails"] += 1
        record["consec_success"] = 0
        if "offline_since" not in record:
            record["offline_since"] = now_iso()

    record["combined"] = combined
    record["last_check"] = now_iso()
    statuses[key] = record

    # ---------------------------
    #  Telegram notifications
    # ---------------------------

    # Transition: ONLINE → OFFLINE
    if prev != "offline" and combined == "offline":
        msg = (
            f"⚠️ <b>{t['name']} OFFLINE</b>\n"
            f"Host: <code>{t['host']}:{t['port']}</code>\n"
            f"Time: <code>{record['offline_since']}</code>"
        )
        send_telegram(msg)

    # Transition: OFFLINE → ONLINE
    if prev != "online" and combined == "online":
        msg = (
            f"🟢 <b>{t['name']} ONLINE</b>\n"
            f"Host: <code>{t['host']}:{t['port']}</code>\n"
            f"Time: <code>{record['last_check']}</code>"
        )
        send_telegram(msg)

save_statuses(statuses)

print("Monitoring cycle completed.")
