#!/usr/bin/env python3
"""
Telegram-бот, що стежить за додатками в Google Play за bundle ID (package name).

Що вміє:
  - Ти надсилаєш боту package name (напр. com.example.app) — він додає його до списку.
  - Кожен запуск (за розкладом з GitHub Actions) бот перевіряє статус кожного додатку.
  - Коли додаток З'ЯВЛЯЄТЬСЯ в сторі — надсилає "додаток вийшов ✅".
  - Коли додаток ЗНИКАЄ / банять — надсилає "додаток недоступний ⛔".

Запускається без сервера — просто скрипт, який раз на кілька хвилин будить GitHub Actions.
Стан зберігається у файлі state.json прямо в репозиторії.
"""

import json
import os
import re
import sys
import time

import requests

# --- Налаштування ---------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    print("ERROR: не задано змінну оточення BOT_TOKEN", file=sys.stderr)
    sys.exit(1)

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# package name виду com.company.app (мінімум одна крапка)
PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")

# Google Play повертає 404, якщо додатку немає / його забанили / зняли.
PLAY_URL = "https://play.google.com/store/apps/details?id={pkg}&hl=en&gl=US"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# --- Робота зі станом -----------------------------------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"offset": 0, "apps": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("offset", 0)
    data.setdefault("apps", {})
    return data


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


# --- Telegram -------------------------------------------------------------

def tg_send(chat_id, text):
    try:
        requests.post(
            f"{API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"send error: {e}", file=sys.stderr)


def tg_get_updates(offset, timeout=0):
    """Забираємо нові повідомлення. timeout>0 = long polling (чекаємо повідомлення)."""
    try:
        r = requests.get(
            f"{API}/getUpdates",
            params={"offset": offset, "timeout": timeout, "allowed_updates": '["message"]'},
            timeout=timeout + 15,
        )
        r.raise_for_status()
        return r.json().get("result", [])
    except requests.RequestException as e:
        print(f"getUpdates error: {e}", file=sys.stderr)
        return []


# --- Перевірка Google Play ------------------------------------------------

def check_play_status(pkg):
    """Повертає 'available', 'unavailable' або 'error' (мережева помилка)."""
    url = PLAY_URL.format(pkg=pkg)
    try:
        r = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
    except requests.RequestException as e:
        print(f"check error {pkg}: {e}", file=sys.stderr)
        return "error"

    if r.status_code == 200 and ("itemprop" in r.text or "og:title" in r.text):
        return "available"
    if r.status_code == 404:
        return "unavailable"
    # 429/5xx/тимчасові збої — не міняємо статус, щоб не було хибних сповіщень
    print(f"check {pkg}: unexpected status {r.status_code}", file=sys.stderr)
    return "error"


# --- Обробка команд -------------------------------------------------------

HELP_TEXT = (
    "Привіт! Я стежу за додатками в Google Play.\n\n"
    "Просто надішли мені package name (bundle ID) додатку, напр.:\n"
    "com.instagram.android\n\n"
    "Я повідомлю, коли додаток з'явиться в сторі, і коли його заблокують/знімуть.\n\n"
    "Команди:\n"
    "/list — показати, за чим я стежу\n"
    "/remove com.example.app — прибрати зі списку\n"
    "/help — ця довідка"
)


def process_updates(updates, state):
    for upd in updates:
        state["offset"] = upd["update_id"] + 1
        msg = upd.get("message")
        if not msg:
            continue
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()
        if not text:
            continue

        if text in ("/start", "/help"):
            tg_send(chat_id, HELP_TEXT)
            continue

        if text == "/list":
            mine = [p for p, v in state["apps"].items() if v.get("chat_id") == chat_id]
            if mine:
                lines = [f"• {p} — {state['apps'][p]['status']}" for p in sorted(mine)]
                tg_send(chat_id, "Я стежу за:\n" + "\n".join(lines))
            else:
                tg_send(chat_id, "Список порожній. Надішли мені package name додатку.")
            continue

        if text.startswith("/remove"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2 and parts[1] in state["apps"]:
                del state["apps"][parts[1]]
                tg_send(chat_id, f"Прибрав {parts[1]} зі списку.")
            else:
                tg_send(chat_id, "Не знайшов такий додаток. Приклад: /remove com.example.app")
            continue

        # інакше вважаємо, що це package name
        pkg = text.lower()
        if not PKG_RE.match(pkg):
            tg_send(
                chat_id,
                "Це не схоже на package name. Приклад правильного: com.example.app",
            )
            continue

        status = check_play_status(pkg)
        state["apps"][pkg] = {"chat_id": chat_id, "status": status}
        if status == "available":
            tg_send(chat_id, f"✅ {pkg} — вже доступний у Google Play. Повідомлю, якщо зникне.")
        elif status == "unavailable":
            tg_send(chat_id, f"⏳ {pkg} — поки недоступний. Повідомлю, щойно вийде в стор.")
        else:
            tg_send(chat_id, f"⚠️ {pkg} — додав, але зараз не зміг перевірити. Перевірю за кілька хвилин.")


# --- Періодична перевірка статусів ---------------------------------------

def monitor(state):
    for pkg, info in list(state["apps"].items()):
        old = info.get("status", "unknown")
        new = check_play_status(pkg)
        if new == "error":
            continue  # тимчасовий збій — статус не чіпаємо
        if new != old:
            info["status"] = new
            chat_id = info["chat_id"]
            if new == "available":
                url = f"https://play.google.com/store/apps/details?id={pkg}"
                tg_send(chat_id, f"🚀 Додаток ВИЙШОВ у Google Play!\n{pkg}\n{url}")
            elif new == "unavailable":
                tg_send(chat_id, f"⛔ Додаток БІЛЬШЕ НЕДОСТУПНИЙ (знято/забанено):\n{pkg}")
        time.sleep(1)  # трохи паузи між запитами до Google


def listen(state, seconds):
    """~`seconds` секунд активно слухаємо повідомлення й миттєво відповідаємо."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        remaining = int(deadline - time.time())
        poll = max(1, min(25, remaining))  # long polling до 25 с
        updates = tg_get_updates(state["offset"], timeout=poll)
        if updates:
            process_updates(updates, state)
            save_state(state)  # одразу зберігаємо, щоб не загубити offset


def main():
    # скільки секунд слухати за один запуск (за замовч. 270 = 4.5 хв,
    # щоб вписатися в 5-хвилинний цикл перезапуску)
    listen_seconds = int(os.environ.get("LISTEN_SECONDS", "270"))

    state = load_state()
    monitor(state)            # перевіряємо статуси додатків
    save_state(state)
    listen(state, listen_seconds)  # далі слухаємо команди в реальному часі
    save_state(state)


if __name__ == "__main__":
    main()
