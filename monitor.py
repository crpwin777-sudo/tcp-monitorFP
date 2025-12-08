#!/usr/bin/env python3
"""
monitor.py — TCP monitor:
- читает hosts.yaml
- сохраняет состояния в statuses.json
- hysteresis (FAIL_THRESHOLD / RECOVERY_THRESHOLD)
- локальные повторы (RETRIES_PER_CHECK)
- отправляет Telegram уведомления (OFFLINE и ONLINE)
- при ONLINE указывает, сколько был офлайн
"""

import os
import socket
import json
import time
from datetime import datetime, timezone
import requests

try:
    import yaml
except Exception:
    yaml = None

# Файлы
HOSTS_FILE = "hosts.yaml"
STATUS_FILE = "statuses.json"

# Секреты / env (в workflow передаём secrets как env)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Параметры мониторинга (можно переопределять через env)
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", "3"))        # N подряд фейлов -> offline
RECOVERY_THRESHOLD = int(os.getenv("RECOVERY_THRESHOLD", "2"))# M подряд успехов -> online
RETRIES_PER_CHECK = int(os.getenv("RETRIES_PER_CHECK", "2"))  # локальные повторы
RETRY_DELAY_SEC = float(os.getenv("RETRY_DELAY_SEC", "0.7"))
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "3.0"))

# Если true — не отправляем в телеграм (для тестов)
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("1","true","yes")

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def load_hosts():
    if not os.path.exists(HOSTS_FILE):
        print(f"[WARN] {HOSTS_FILE} not found")
        return []
    if yaml is None:
        print("[ERROR] pyyaml not installed")
        return []
    with open(HOSTS_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
        out = []
        for item in data:
            if not item:
                continue
            enabled = item.get("enabled", True)
            if isinstance(enabled, str):
                enabled = enabled.lower() not in ("false", "0", "no")
            if not enabled:
                continue
            name = item.get("name") or f"{item.get('host')}:{item.get('port')}"
            host = item.get("host")
            port = int(item.get("port") or 3389)
            out.append({"name": str(name), "host": str(host), "port": int(port)})
        return out

def load_statuses():
    if not os.path.exists(STATUS_FILE):
        return {}
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("[WARN] failed to load statuses.json:", e)
        return {}

def save_statuses(statuses):
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(statuses, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print("[ERROR] failed to save statuses.json:", e)

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
        print("[INFO] Telegram not configured. Would send:", text)
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print("[TG] status:", r.status_code, "resp:", r.text)
        return r
    except Exception as e:
        print("[ERROR] Failed to send telegram:", e)
        return None

def format_duration_since(iso_ts):
    if not iso_ts:
        return "?"
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
        return "?"

def main():
    hosts = load_hosts()
    statuses = load_statuses()

    # Обрабатывать все хосты из hosts.yaml — это даёт корректные имена
    for h in hosts:
        name = h["name"]
        host = h["host"]
        port = int(h["port"])
        key = f"{host}:{port}"

        prev = statuses.get(key, {})
        prev_combined = prev.get("combined", "")
        prev_offline_since = prev.get("offline_since")

        # Текущая проверка TCP с повторами
        is_online = tcp_check_with_retries(host, port, retries=RETRIES_PER_CHECK)
        temp_combined = "online" if is_online else "offline"

        # Инициализация записи, если нет
        rec = statuses.get(key, {
            "name": name,
            "host": host,
            "port": port,
            "combined": temp_combined,
            "consec_fails": 0,
            "consec_success": 0,
            "offline_since": None,
            "last_check": None
        })

        # Если имя в hosts.yaml поменялось — обновим его в записи
        if rec.get("name") != name:
            print(f"[INFO] Update stored name for {key}: '{rec.get('name')}' -> '{name}'")
            rec["name"] = name

        # Обновляем счётчики и offline_since
        if temp_combined == "online":
            rec["consec_success"] = (rec.get("consec_success") or 0) + 1
            rec["consec_fails"] = 0
        else:
            rec["consec_fails"] = (rec.get("consec_fails") or 0) + 1
            rec["consec_success"] = 0
            if not rec.get("offline_since"):
                rec["offline_since"] = now_iso()

        rec["combined"] = temp_combined
        rec["last_check"] = now_iso()
        statuses[key] = rec

        # --- Логика уведомлений: отправлять только при переходе состояния после достижения порога ---
        # Переход в OFFLINE (если ранее не offline, и достигнут FAIL_THRESHOLD)
        if temp_combined == "offline" and prev_combined != "offline":
            if rec["consec_fails"] >= FAIL_THRESHOLD:
                msg = (f"⚠️ <b>{rec['name']} OFFLINE</b>\n"
                       f"Host: <code>{rec['host']}:{rec['port']}</code>\n"
                       f"Time: <code>{rec['offline_since']}</code>\n"
                       f"Consecutive fails: {rec['consec_fails']}")
                if DRY_RUN:
                    print("[DRY_RUN] would send:", msg)
                else:
                    send_telegram(msg)

        # Переход в ONLINE (если ранее offline и достигнут RECOVERY_THRESHOLD) — при этом указываем длительность простоя
        if temp_combined == "online" and prev_combined == "offline":
            if rec["consec_success"] >= RECOVERY_THRESHOLD:
                downtime = format_duration_since(rec.get("offline_since"))
                msg = (f"🟢 <b>{rec['name']} ONLINE</b>\n"
                       f"Host: <code>{rec['host']}:{rec['port']}</code>\n"
                       f"Time: <code>{rec['last_check']}</code>\n"
                       f"Was offline: {downtime}")
                if DRY_RUN:
                    print("[DRY_RUN] would send:", msg)
                else:
                    send_telegram(msg)
                # очистим offline_since
                rec["offline_since"] = None

        # Печать статуса в лог
        print(f"[INFO] {rec['name']} {key} -> {rec['combined']} (fails={rec.get('consec_fails')} succ={rec.get('consec_success')})")

    # Удалим из statuses ключи, которые больше не присутствуют в hosts.yaml (чтобы не расти бесконтрольно)
    current_keys = set(f"{h['host']}:{h['port']}" for h in hosts)
    removed = []
    for k in list(statuses.keys()):
        if k not in current_keys:
            removed.append(k)
            del statuses[k]
    if removed:
        print("[INFO] Removed stale statuses for keys:", removed)

    save_statuses(statuses)
    print("[DONE] Statuses saved to", STATUS_FILE)


if __name__ == "__main__":
    main()
