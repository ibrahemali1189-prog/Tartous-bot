import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

PORT_URL = "https://www.myshiptracking.com/ports/port-of-tartous-in-sy-syria-id-3148"
STATE_FILE = "seen.json"
TZ = ZoneInfo("Asia/Damascus")
DAILY_SUMMARY_HOUR = 8  # send once, in the 8:00-8:09 run

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
# Supports one or more chat IDs, comma-separated, e.g. "111111,222222"
CHAT_IDS = [c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()]

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


def _parse_table_by_headers(soup, required_headers):
    """Generic helper: find a table whose <th> headers contain all of
    required_headers, and return (headers, list_of_row_cell_texts)."""
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if all(h in headers for h in required_headers):
            rows = []
            for tr in table.find_all("tr")[1:]:
                cells = tr.find_all("td")
                if not cells:
                    continue
                row = [c.get_text(" ", strip=True) for c in cells]
                rows.append(row)
            return headers, rows
    return None, []


def fetch_in_port(soup):
    """Scrape the 'Vessels In Port' table, including DWT (cargo capacity)."""
    headers, rows = _parse_table_by_headers(soup, ["Vessel", "Arrived"])
    vessels = []
    for row in rows:
        rowmap = dict(zip(headers, row))
        name = rowmap.get("Vessel", "")
        if not name:
            continue
        vessels.append({
            "name": name,
            "arrived": rowmap.get("Arrived", ""),
            "dwt": rowmap.get("DWT", ""),
            "grt": rowmap.get("GRT", ""),
            "built": rowmap.get("Built", ""),
            "size": rowmap.get("Size", ""),
        })
    return vessels


def fetch_expected(soup):
    """Scrape the 'Expected Arrivals' table from the port page."""
    headers, rows = _parse_table_by_headers(soup, ["Vessel", "Estimated Arrival"])
    vessels = []
    for row in rows:
        rowmap = dict(zip(headers, row))
        name = rowmap.get("Vessel", "")
        if not name:
            continue
        vessels.append({
            "name": name,
            "eta": rowmap.get("Estimated Arrival", ""),
        })
    return vessels


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                # old format migration
                return {"seen": data, "update_offset": 0, "last_summary_date": ""}
            data.setdefault("update_offset", 0)
            data.setdefault("last_summary_date", "")
            return data
    return {"seen": [], "update_offset": 0, "last_summary_date": ""}


def save_state(state):
    state["seen"] = sorted(set(state["seen"]))[-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for chat_id in CHAT_IDS:
        r = requests.post(url, data={"chat_id": chat_id, "text": msg}, timeout=30)
        r.raise_for_status()


def get_new_commands(state):
    """Check for new commands sent to the bot since the last run.
    Accepts commands from any of the configured chat IDs."""
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
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id not in CHAT_IDS:
            continue
        low = text.lower()
        if low.startswith("/inport") or low.startswith("/expected") or low.startswith("/ship"):
            commands.append(text)

    state["update_offset"] = max_id
    return commands


def format_vessel_line(v):
    extra = []
    if v.get("dwt"):
        extra.append(f"cargo: {v['dwt']}")
    if v.get("built"):
        extra.append(f"built: {v['built']}")
    extra_txt = f" ({', '.join(extra)})" if extra else ""
    arrived_txt = f" — arrived: {v['arrived']}" if v.get("arrived") else ""
    return f"🚢 {v['name']}{arrived_txt}{extra_txt}"


def handle_in_port_request(soup):
    vessels = fetch_in_port(soup)
    if not vessels:
        send_telegram("Couldn't fetch the current vessel list right now, try again shortly.")
        return
    lines = ["⚓ Vessels currently in the Port of Tartous:\n"]
    for v in vessels:
        lines.append(format_vessel_line(v))
    send_telegram("\n".join(lines))


def handle_expected_request(soup):
    vessels = fetch_expected(soup)
    if not vessels:
        send_telegram("No expected arrivals currently listed for the Port of Tartous.")
        return
    lines = ["🕒 Vessels expected to arrive at the Port of Tartous:\n"]
    for v in vessels:
        eta_txt = f" — ETA: {v['eta']}" if v.get("eta") else ""
        lines.append(f"🚢 {v['name']}{eta_txt}")
    send_telegram("\n".join(lines))


def handle_ship_request(soup, query):
    query = query.strip().lower()
    if not query:
        send_telegram("Add a ship name after the command, e.g.: /ship EVER GIVEN")
        return

    in_port = fetch_in_port(soup)
    expected = fetch_expected(soup)
    activity = fetch_activity(soup)

    matches_in_port = [v for v in in_port if query in v["name"].lower()]
    matches_expected = [v for v in expected if query in v["name"].lower()]
    # fallback: search recent activity log too, since the in-port table on
    # the main page only shows the first ~10 vessels out of the full count
    matches_activity = [
        e for e in activity
        if query in e["vessel"].lower()
        and e["vessel"].lower() not in {v["name"].lower() for v in matches_in_port}
    ]

    if not matches_in_port and not matches_expected and not matches_activity:
        send_telegram(f"No vessel matching \"{query}\" found for the Port of Tartous right now.")
        return

    lines = []
    for v in matches_in_port:
        lines.append("📍 Currently in port:")
        lines.append(format_vessel_line(v))
    for v in matches_expected:
        eta_txt = f" — ETA: {v['eta']}" if v.get("eta") else ""
        lines.append("🕒 Expected to arrive:")
        lines.append(f"🚢 {v['name']}{eta_txt}")
    # de-duplicate activity matches by vessel name, keep most recent only
    seen_names = set()
    for e in matches_activity:
        if e["vessel"] in seen_names:
            continue
        seen_names.add(e["vessel"])
        icon = "🟢 Arrived" if e["event"] == "ARRIVAL" else "🔴 Departed"
        lines.append(f"{icon} (per latest recorded activity):")
        lines.append(f"🚢 {e['vessel']} — {e['time']}")
    send_telegram("\n".join(lines))


def maybe_send_daily_summary(soup, state):
    now = datetime.now(TZ)
    today_str = now.strftime("%Y-%m-%d")
    # send on the first run of the day at/after the target hour, rather than
    # requiring an exact hour match - free GitHub Actions schedules can run
    # late by hours, so a strict match can skip the day entirely
    if now.hour < DAILY_SUMMARY_HOUR:
        return
    if state.get("last_summary_date") == today_str:
        return

    in_port = fetch_in_port(soup)
    expected = fetch_expected(soup)

    lines = [f"📋 Daily Summary - Port of Tartous ({today_str})\n"]
    lines.append(f"⚓ Vessels currently in port: {len(in_port)}")
    lines.append(f"🕒 Vessels expected to arrive: {len(expected)}\n")

    if in_port:
        lines.append("Vessels in port:")
        for v in in_port:
            lines.append(format_vessel_line(v))

    send_telegram("\n".join(lines))
    state["last_summary_date"] = today_str


def page_looks_valid(soup):
    """Basic sanity check: bail out if the fetched page looks broken/empty
    (e.g. a temporary site glitch) rather than trusting bad data."""
    text = soup.get_text()
    if "No Internet" in text and "Vessels In Port" not in text:
        return False
    return True


def main():
    soup = fetch_page()

    if not page_looks_valid(soup):
        # site returned a broken/incomplete page this run - skip everything
        # rather than risk sending false notifications, try again next run
        return

    state = load_state()
    seen = set(state.get("seen", []))
    first_run = len(seen) == 0

    in_port_names = {v["name"].lower() for v in fetch_in_port(soup)}

    # 1) check for new ships arriving/leaving
    events = fetch_activity(soup)
    new_events = [e for e in events if e["key"] not in seen]

    # Confirm any candidate notifications with a second, independent fetch
    # a few seconds later - only report events that show up consistently in
    # both, to filter out transient site glitches (e.g. a broken page load).
    confirmed_keys = set()
    if new_events and not first_run:
        soup2 = fetch_page()
        if page_looks_valid(soup2):
            confirmed_keys = {e2["key"] for e2 in fetch_activity(soup2)}

    for e in reversed(new_events):  # oldest first
        if not first_run and e["key"] not in confirmed_keys:
            continue  # didn't reappear on the confirmation fetch - retry next run

        if first_run:
            seen.add(e["key"])
            continue  # don't spam on the very first run, just record history

        # extra sanity check: if the source claims a vessel departed but it's
        # still listed as in-port right now, treat it as a bad scrape and
        # retry next run instead of trusting it.
        if e["event"] == "DEPARTURE" and e["vessel"].lower() in in_port_names:
            continue

        seen.add(e["key"])
        icon = "🟢 Arrived" if e["event"] == "ARRIVAL" else "🔴 Departed"
        msg = f"{icon}: {e['vessel']}\n🕒 Time: {e['time']}\n⚓ Port of Tartous"
        send_telegram(msg)

    state["seen"] = list(seen)

    # 2) check if the user sent a command
    commands = get_new_commands(state)
    for cmd in commands:
        low = cmd.lower()
        if low.startswith("/inport"):
            handle_in_port_request(soup)
        elif low.startswith("/expected"):
            handle_expected_request(soup)
        elif low.startswith("/ship"):
            query = cmd[len("/ship"):].strip()
            handle_ship_request(soup, query)

    # 3) daily summary (once per day, around 8 AM Damascus time)
    if not first_run:
        maybe_send_daily_summary(soup, state)

    save_state(state)


if __name__ == "__main__":
    main()
