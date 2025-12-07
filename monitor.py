#!/usr/bin/env python3
# monitor.py — GitHub Actions TCP monitor with hysteresis + telegram alerts + state stored in statuses.json

import socket
import json
import yaml
import time
import os
from datetime import datetime, timezone

# Настройки (можно менять через env или прямо тут)
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", "3"))       # N подряд падений -> offline
RECOVERY_THRESHOLD = int(os.getenv("RECOVERY_THRESHOLD", "2"))  # M подряд успехов -> online
RETRIES_PER_CHECK = int(os.getenv("RETRIES_PER_CHECK", "2"))   # локальные повторы при одной проверке
RETRY_DELAY_SEC = float(os.getenv("RETRY_DELAY_SEC", "0.7"))
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "3.0"))

HOSTS_FILE = "hosts.yaml"
STATUSES_FILE = "statuses.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("1","true","yes")

def now_ts():
    return datetime.now(timezone.utc).isoformat()

def load_hosts():
    with open(HOSTS_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
        if not isinstance(data, list):
            return []
        return data

def load_statuses():
    if not os.path.exists(STATUSES_FILE):
        return {}
    with open(STATUSES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_statuses(statuses):
    with open(STATUSES_FILE, "w", encoding="utf-8") as f:
        json.dump(statuses, f, indent=2, ensure_ascii=False)

def tcp_check_once(host, port, timeout=CONNECT_TIMEOUT):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False

def tcp_check(host, port, retries=RETRIES_PER_CHECK):
    last = False
    for i in range(max(1, retries)):
        ok = tcp_check_once(host, port)
        if ok:
            return True
        last = ok
        time.sleep(RETRY_DELAY_SEC)
    return last

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured, would send:", text)
        return
    import requests
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        print("Telegram sent:", text)
    except Exception as e:
        print("Failed to send Telegram:", e)

def format_duration_iso(iso_ts):
    if not iso_ts:
        return ""
    try:
        t0 = datetime.fromisoformat(iso_ts)
        diff = datetime.now(timezone.utc) - t0
        total = int(diff.total_seconds())
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        if h > 0:
            return f"{h}h {m}m {s}s"
        if m > 0:
            return f"{m}m {s}s"
        return f"{s}s"
    except Exception:
        return ""

def main():
    hosts = load_hosts()
    statuses = load_statuses()  # dict keyed by identifier (name|host:port)
    updated = False

    for h in hosts:
        if not h.get("enabled", True):
            continue
        name = h.get("name") or f"{h.get('host')}:{h.get('port')}"
        host = h["host"]
        port = int(h["port"])
        key = f"{host}:{port}"

        prev = statuses.get(key, {
            "name": name,
            "host": host,
            "port": port,
            "combined": "",
            "consec_fails": 0,
            "consec_success": 0,
            "offline_since": None,
            "last_check": None
        })

        # run tcp check with retries
        is_online = tcp_check(host, port, retries=RETRIES_PER_CHECK)
        temp_combined = "online" if is_online else "offline"

        # hysteresis counters
        if temp_combined == "offline":
            prev["consec_fails"] = (prev.get("consec_fails") or 0) + 1
            prev["consec_success"] = 0
        else:
            prev["consec_success"] = (prev.get("consec_success") or 0) + 1
            prev["consec_fails"] = 0

        # decide state transitions
        old_combined = prev.get("combined") or ""
        new_combined = old_combined

        # if currently online or unknown, become offline only after FAIL_THRESHOLD
        if old_combined in ("", "online"):
            if prev["consec_fails"] >= FAIL_THRESHOLD:
                new_combined = "offline"
                if not prev.get("offline_since"):
                    prev["offline_since"] = now_ts()
                    # send alert only if previous known online
                    if old_combined == "online" and not DRY_RUN:
                        send_telegram(f"🔴 OFFLINE: <b>{name}</b> ({host}:{port}) — стал недоступен")
        # if currently offline, become online only after RECOVERY_THRESHOLD
        if old_combined == "offline":
            if prev["consec_success"] >= RECOVERY_THRESHOLD:
                new_combined = "online"
                if prev.get("offline_since"):
                    dur = format_duration_iso(prev["offline_since"])
                    if not DRY_RUN:
                        send_telegram(f"🟢 ONLINE: <b>{name}</b> ({host}:{port}) снова доступен\n⏱ Был офлайн: {dur}")
                    prev["offline_since"] = None
                else:
                    if not DRY_RUN:
                        send_telegram(f"🟢 ONLINE: <b>{name}</b> ({host}:{port}) снова доступен")

        # If first run (old_combined == "") we do not send alerts until threshold reached; above logic respects that.

        prev["combined"] = new_combined
        prev["last_check"] = now_ts()

        statuses[key] = prev
        updated = True

        print(f"{name} {host}:{port} temp={temp_combined} combined={prev['combined']} fails={prev['consec_fails']} succ={prev['consec_success']} last={prev['last_check']}")

    # save statuses
    if updated:
        save_statuses(statuses)
        print("Saved statuses.")
    else:
        print("No hosts found or no updates.")

if __name__ == "__main__":
    main()
