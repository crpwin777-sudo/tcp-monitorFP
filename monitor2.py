#!/usr/bin/env python3
# monitor.py — TCP monitor reading hosts.yaml, saving statuses.json, sending colored TG alerts

import socket
import json
import os
import time
from datetime import datetime, timezone
import requests

try:
    import yaml
except Exception:
    yaml = None  # workflow должен установить pyyaml

HOSTS_FILE = "hosts.yaml"
STATUS_FILE = "statuses.json"

# Env secrets names (в workflow должны быть переданы именно эти secrets)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Параметры (можно переопределять через env в workflow)
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", "3"))
RECOVERY_THRESHOLD = int(os.getenv("RECOVERY_THRESHOLD", "2"))
RETRIES_PER_CHECK = int(os.getenv("RETRIES_PER_CHECK", "2"))
RETRY_DELAY_SEC = float(os.getenv("RETRY_DELAY_SEC", "0.7"))
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "3.0"))

DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("1","true","yes")

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def load_hosts():
    # читаем hosts.yaml; формат: список объектов {name, host, port, enabled:true/false}
    if not os.path.exists(HOSTS_FILE) or yaml is None:
        return []
    with open(HOSTS_FILE, "r", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
            if isinstance(data, list):
                # фильтруем включённые
                out = []
                for item in data:
                    if not item:
                        continue
                    if "enabled" in item and item.get("enabled") in (False, "false", "no", 0):
                        continue
                    # нормализуем поля
                    name = item.get("name") or f"{item.get('host')}:{item.get('port')}"
                    host = item.get("host")
                    port = int(item.get("port") or 3389)
                    out.append({"name": str(name), "host": str(host), "port": int(port)})
                return out
        except Exception as e:
            print("Failed parse hosts.yaml:", e)
            return []
    return []

def load_statuses():
    if not os.path.exists(STATUS_FILE):
        return {}
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("Failed to load statuses.json:", e)
        return {}

def save_statuses(statuses):
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(statuses, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print("Failed to save statuses.json:", e)

def tcp_once(host, port, timeout=CONNECT_TIMEOUT):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False

def tcp_check_with_retries(host, port, retries=RETRIES_PER_CHECK):
    last = False
    for i in range(max(1, retries)):
        ok = tcp_once(host, port)
        if ok:
            return True
        last = ok
        time.sleep(RETRY_DELAY_SEC)
    return last

def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured, would send:", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print("TG response:", r.status_code, r.text)
        return r
    except Exception as e:
        print("Failed to send TG:", e)
        return None

def format_duration(iso_ts):
    if not iso_ts:
        return ""
    try:
        t0 = datetime.fromisoformat(iso_ts)
        diff = datetime.now(timezone.utc) - t0
        s = int(diff.total_seconds())
        h = s // 3600
        m = (s % 3600) // 60
        sec = s % 60
        if h > 0:
            return f"{h}ч {m}м {sec}с"
        if m > 0:
            return f"{m}м {sec}с"
        return f"{sec}с"
    except Exception:
        return ""

def main():
    hosts = load_hosts()
    if not hosts:
        print("No hosts found in hosts.yaml or pyyaml not installed.")
    statuses = load_statuses()

    # process each host from hosts.yaml — this ensures name comes from hosts.yaml
    for h in hosts:
        name = h["name"]
        host = h["host"]
        port = int(h["port"])
        key = f"{host}:{port}"

        prev = statuses.get(key, {})
        prev_combined = prev.get("combined", "")
        prev_offline_since = prev.get("offline_since")

        # run tcp check
        is_online = tcp_check_with_retries(host, port, retries=RETRIES_PER_CHECK)
        temp = "online" if is_online else "offline"

        # initialize record if missing
        rec = statuses.get(key, {
            "name": name,
            "host": host,
            "port": port,
            "combined": temp,
            "consec_fails": 0,
            "consec_success": 0,
            "offline_since": None,
            "last_check": None
        })

        # If name changed in hosts.yaml — update stored name so notifications use new name
        if rec.get("name") != name:
            print(f"Updating stored name for {key}: '{rec.get('name')}' -> '{name}'")
            rec["name"] = name

        # update counters & offline_since
        if temp == "online":
            rec["consec_success"] = (rec.get("consec_success") or 0) + 1
            rec["consec_fails"] = 0
        else:
            rec["consec_fails"] = (rec.get("consec_fails") or 0) + 1
            rec["consec_success"] = 0
            if not rec.get("offline_since"):
                rec["offline_since"] = now_iso()

        rec["combined"] = temp
        rec["last_check"] = now_iso()

        # decide transitions: send alerts only on transition (prev_combined != current) and when threshold reached
        # If previously unknown/empty, still send only when threshold reached
        # transition to offline
        if temp == "offline" and prev_combined != "offline":
            # check threshold
            if rec["consec_fails"] >= FAIL_THRESHOLD:
                # send alert
                msg = (f"⚠️ <b>{rec['name']} OFFLINE</b>\n"
                       f"Host: <code>{rec['host']}:{rec['port']}</code>\n"
                       f"Time: <code>{rec['offline_since']}</code>\n"
                       f"Consec fails: {rec['consec_fails']}")
                if not DRY_RUN:
                    send_telegram(msg)
                else:
                    print("DRY_RUN; would send:", msg)
        # transition to online
        if temp == "online" and prev_combined == "offline":
            if rec["consec_success"] >= RECOVERY_THRESHOLD:
                # compute downtime
                down = format_duration(rec.get("offline_since")) if rec.get("offline_since") else "?"
                msg = (f"🟢 <b>{rec['name']} ONLINE</b>\n"
                       f"Host: <code>{rec['host']}:{rec['port']}</code>\n"
                       f"Time: <code>{rec['last_check']}</code>\n"
                       f"Was offline: {down}")
                if not DRY_RUN:
                    send_telegram(msg)
                else:
                    print("DRY_RUN; would send:", msg)
                # clear offline_since
                rec["offline_since"] = None

        # save back
        statuses[key] = rec
        print(f"{rec['name']} {key} -> {rec['combined']} (fails={rec.get('consec_fails')} succ={rec.get('consec_success')})")

    # ALSO: remove entries from statuses.json that are not present in hosts.yaml (optional)
    # We'll keep statuses only for current hosts:
    current_keys = set(f"{h['host']}:{h['port']}" for h in hosts)
    removed = []
    for k in list(statuses.keys()):
        if k not in current_keys:
            removed.append(k)
            del statuses[k]
    if removed:
        print("Removed stale statuses for keys:", removed)

    save_statuses(statuses)
    print("Done. Statuses saved to", STATUS_FILE)

if __name__ == "__main__":
    main()
