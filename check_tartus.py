import json
import os

import requests
from bs4 import BeautifulSoup

PORT_URL = "https://www.myshiptracking.com/ports/port-of-tartous-in-sy-syria-id-3148"
STATE_FILE = "seen.json"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def fetch_activity():
    """Scrape the ARRIVAL/DEPARTURE activity table from the port page."""
    resp = requests.get(PORT_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    events = []
    for tr in soup.find_all("tr"):
        text = tr.get_text(" ", strip=True)
        if "ARRIVAL" not in text and "DEPARTURE" not in text:
            continue
        cells = tr.find_all("td")
        if len(cells) < 3:
            continue
        time_txt = cells[0].get_text(strip=True)
        event_txt = "ARRIVAL" if "ARRIVAL" in cells[1].get_text() else "DEPARTURE"
        vessel_txt = cells[2].get_text(" ", strip=True)
        if not vessel_txt:
            continue
        key = f"{time_txt}|{event_txt}|{vessel_txt}"
        events.append({"key": key, "time": time_txt, "event": event_txt, "vessel": vessel_txt})
    return events


def load_seen():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    # keep the file from growing forever
    trimmed = sorted(seen)[-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False, indent=2)


def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=30)
    r.raise_for_status()


def main():
    events = fetch_activity()
    seen = load_seen()
    first_run = len(seen) == 0

    new_events = [e for e in events if e["key"] not in seen]

    # oldest first so messages arrive in chronological order
    for e in reversed(new_events):
        seen.add(e["key"])
        if first_run:
            # don't spam on the very first run, just record history
            continue
        icon = "🟢 دخول" if e["event"] == "ARRIVAL" else "🔴 خروج"
        msg = f"{icon} سفينة: {e['vessel']}\n🕒 الوقت: {e['time']}\n⚓ ميناء طرطوس"
        send_telegram(msg)

    save_seen(seen)


if __name__ == "__main__":
    main()
