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


def fetch_page():
    resp = requests.get(PORT_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def fetch_activity(soup):
    """Scrape the ARRIVAL/DEPARTURE activity table from the port page."""
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


def fetch_in_port(soup):
    """Scrape the 'Vessels In Port' table from the port page."""
    vessels = []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if "Vessel" in headers and "Arrived" in headers:
            for tr in table.find_all("tr")[1:]:
                cells = tr.find_all("td")
                if not cells:
                    continue
                name = cells[0].get_text(" ", strip=True)
                arrived = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                if name:
                    vessels.append((name, arrived))
            break
    return vessels


def fetch_expected(soup):
    """Scrape the 'Expected Arrivals' table from the port page."""
    vessels = []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if "Vessel" in headers and "Estimated Arrival" in headers:
            for tr in table.find_all("tr")[1:]:
                cells = tr.find_all("td")
                if not cells:
                    continue
                name = cells[1].get_text(" ", strip=True) if len(cells) > 1 else ""
                eta = cells[2].get_text(strip=True) if len(cells) > 2 else ""
                if name:
                    vessels.append((name, eta))
            break
    return vessels


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                # old format migration
                return {"seen": data, "update_offset": 0}
            return data
    return {"seen": [], "update_offset": 0}


def save_state(state):
    state["seen"] = sorted(set(state["seen"]))[-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=30)
    r.raise_for_status()


def get_new_commands(state):
    """Check for new /inport commands sent to the bot since the last run."""
    offset = state.get("update_offset", 0)
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"offset": offset + 1, "timeout": 0}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()

    commands = []
    max_id = offset
    for update in data.get("result", []):
        max_id = max(max_id, update["update_id"])
        msg = update.get("message", {})
        text = msg.get("text", "").strip().lower()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id == CHAT_ID and (text.startswith("/inport") or text.startswith("/expected")):
            commands.append(text)

    state["update_offset"] = max_id
    return commands


def handle_in_port_request(soup):
    vessels = fetch_in_port(soup)
    if not vessels:
        send_telegram("ما قدرت أجيب لستة السفن الحالية حالياً، جرب بعد شوي.")
        return
    lines = ["⚓ السفن الراسية حالياً بميناء طرطوس:\n"]
    for name, arrived in vessels:
        if arrived:
            lines.append(f"🚢 {name} — وصلت: {arrived}")
        else:
            lines.append(f"🚢 {name}")
    send_telegram("\n".join(lines))


def handle_expected_request(soup):
    vessels = fetch_expected(soup)
    if not vessels:
        send_telegram("ما في سفن متوقع وصولها مسجلة حالياً بميناء طرطوس.")
        return
    lines = ["🕒 السفن المتوقع وصولها لميناء طرطوس:\n"]
    for name, eta in vessels:
        if eta:
            lines.append(f"🚢 {name} — الوصول المتوقع: {eta}")
        else:
            lines.append(f"🚢 {name}")
    send_telegram("\n".join(lines))


def main():
    soup = fetch_page()
    state = load_state()
    seen = set(state.get("seen", []))
    first_run = len(seen) == 0

    # 1) check for new ships arriving/leaving
    events = fetch_activity(soup)
    new_events = [e for e in events if e["key"] not in seen]
    for e in reversed(new_events):  # oldest first
        seen.add(e["key"])
        if first_run:
            continue  # don't spam on the very first run, just record history
        icon = "🟢 دخول" if e["event"] == "ARRIVAL" else "🔴 خروج"
        msg = f"{icon} سفينة: {e['vessel']}\n🕒 الوقت: {e['time']}\n⚓ ميناء طرطوس"
        send_telegram(msg)

    state["seen"] = list(seen)

    # 2) check if the user sent a command
    commands = get_new_commands(state)
    for cmd in commands:
        if cmd.startswith("/inport"):
            handle_in_port_request(soup)
        elif cmd.startswith("/expected"):
            handle_expected_request(soup)

    save_state(state)


if __name__ == "__main__":
    main()
