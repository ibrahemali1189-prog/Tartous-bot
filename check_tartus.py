import json
import logging
import os
import re
from datetime import datetime, timedelta
from math import radians, cos, hypot
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

PORT_URL = "https://www.myshiptracking.com/ports/port-of-tartous-in-sy-syria-id-3148"
STATE_FILE = "seen.json"
TZ = ZoneInfo("Asia/Damascus")
DAILY_SUMMARY_HOUR = 8
ZONE_DAILY_SUMMARY_HOUR = 7
EXPECTED_UPDATE_INTERVAL = timedelta(hours=3)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("tartous_bot")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_IDS = [c.strip() for c in os.environ["TELEGRAM_CHAT_ID"].split(",") if c.strip()]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

BERTHS = {
    "4": ((34.910513, 35.869054), (34.908821, 35.862661)),
    "7": ((34.909062, 35.869411), (34.907714, 35.864136)),
    "9": ((34.907516, 35.870729), (34.905612, 35.863220)),
    "12": ((34.906586, 35.872711), (34.904655, 35.865079)),
    "13": ((34.902991, 35.865084), (34.904489, 35.864903)),
    "14": ((34.904985, 35.873420), (34.902955, 35.865103)),
    "22": ((34.900927, 35.872745), (34.900384, 35.872566)),
}
_BERTH_MAX_DISTANCE_M = 120
_REF_LAT = 34.905


def bounding_box(center_lat, center_lon, km_north=0, km_south=0, km_west=0, km_east=0):
    """Build a rectangular lat/lon box from a center point and distances (km)
    in each cardinal direction. Any distance left at 0 means the box does not
    extend that way (the center's own coordinate is used as that edge)."""
    lat_max = center_lat + km_north / 110.540
    lat_min = center_lat - km_south / 110.540
    lon_per_km = 1 / (111.320 * cos(radians(center_lat)))
    lon_min = center_lon - km_west * lon_per_km
    lon_max = center_lon + km_east * lon_per_km
    return {"lat_min": lat_min, "lat_max": lat_max, "lon_min": lon_min, "lon_max": lon_max}


# Custom watch zone: 2km north + 2km south of the center point, and 4km west
# of it (no eastward extension, so the east edge sits on the center point's
# own longitude). Adjust the numbers here if the zone needs to change.
ZONE_A_LABEL = "الياطر"
ZONE_A = bounding_box(34.907124, 35.853418, km_north=2, km_south=2, km_west=4, km_east=0)


def point_in_zone(lat, lon, zone):
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return False
    return zone["lat_min"] <= lat <= zone["lat_max"] and zone["lon_min"] <= lon <= zone["lon_max"]


def _to_xy(lat, lon):
    x = (lon - 35.868) * 111320 * cos(radians(_REF_LAT))
    y = (lat - _REF_LAT) * 110540
    return x, y


def _point_segment_distance(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0, min(1, t))
    proj_x, proj_y = x1 + t * dx, y1 + t * dy
    return hypot(px - proj_x, py - proj_y)


def nearest_berth(lat, lon):
    try:
        px, py = _to_xy(float(lat), float(lon))
    except (TypeError, ValueError):
        return None, None
    best_name, best_dist = None, None
    for name, (p1, p2) in BERTHS.items():
        x1, y1 = _to_xy(*p1)
        x2, y2 = _to_xy(*p2)
        d = _point_segment_distance(px, py, x1, y1, x2, y2)
        if best_dist is None or d < best_dist:
            best_name, best_dist = name, d
    if best_dist is not None and best_dist <= _BERTH_MAX_DISTANCE_M:
        return best_name, round(best_dist)
    return None, None


def extract_vessel_id(url):
    if not url:
        return None
    match = re.search(r"-id-(\d+)", url)
    if match:
        return match.group(1)
    match_mmsi = re.search(r"mmsi-(\d+)", url)
    return match_mmsi.group(1) if match_mmsi else None


def fetch_page(url=PORT_URL):
    """Fetch and parse a page. Returns None (and logs) on any network failure
    instead of letting the exception propagate and crash the whole run."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        logger.error("Failed to fetch page %s: %s", url, exc)
        return None


def fetch_activity(soup):
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
        link = cells[2].find("a")
        vessel_url = link["href"] if link and link.get("href") else None
        if vessel_url and vessel_url.startswith("/"):
            vessel_url = "https://www.myshiptracking.com" + vessel_url

        v_id = extract_vessel_id(vessel_url) or vessel_txt
        # Key is based on vessel id + event type + reported event time,
        # to prevent duplicate/stale notifications.
        key = f"{v_id}|{event_txt}|{time_txt}"

        events.append({
            "key": key, "time": time_txt, "event": event_txt,
            "vessel": vessel_txt, "url": vessel_url,
        })
    return events


def fetch_vessel_details(vessel_url):
    """Fetch a vessel's page once and extract both its type and its latest
    reported position from the same soup, instead of making two separate
    requests (one from fetch_vessel_type, one from fetch_vessel_position)."""
    result = {"type": None, "lat": None, "lon": None, "reported": None}
    if not vessel_url:
        return result

    try:
        resp = requests.get(vessel_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Failed to fetch vessel page %s: %s", vessel_url, exc)
        return result

    vsoup = BeautifulSoup(resp.text, "html.parser")

    # --- vessel type ---
    h2 = vsoup.find("h2")
    if h2:
        type_txt = h2.get_text(strip=True)
        if type_txt:
            result["type"] = type_txt
    if result["type"] is None:
        logger.warning("Could not find vessel type on %s", vessel_url)

    # --- position: try the Time/Event table first ---
    found_position = False
    for table in vsoup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if "Time" in headers and "Event" in headers:
            rows = table.find_all("tr")[1:]
            if rows:
                cells = rows[0].find_all("td")
                if cells:
                    time_txt = cells[0].get_text(strip=True)
                    row_txt = rows[0].get_text(" ", strip=True)
                    pmatch = re.search(r"(-?\d+\.\d+)\s*/\s*(-?\d+\.\d+)", row_txt)
                    if pmatch:
                        lat, lon = pmatch.groups()
                        result["lat"], result["lon"], result["reported"] = lat, lon, time_txt
                        found_position = True
            break

    # --- fallback: try the free-text "coordinates x/y as reported on ..." pattern ---
    if not found_position:
        text = vsoup.get_text(" ", strip=True)
        match = re.search(
            r"coordinates\s+(-?\d+\.\d+)\s*[°]?\s*/\s*(-?\d+\.\d+)\s*[°]?\s*"
            r"as reported on\s+([0-9:\-\s]+?)\s+by AIS",
            text,
        )
        if match:
            lat, lon, reported = match.groups()
            result["lat"], result["lon"], result["reported"] = lat, lon, reported.strip()
            found_position = True

    if not found_position:
        logger.warning(
            "Could not extract position for %s (page layout may have changed)",
            vessel_url,
        )

    return result


def _parse_table_by_headers(soup, required_headers):
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if all(h in headers for h in required_headers):
            rows = []
            for tr in table.find_all("tr")[1:]:
                cells = tr.find_all("td")
                if not cells:
                    continue
                row = []
                for c in cells:
                    text = c.get_text(" ", strip=True)
                    link = c.find("a")
                    href = link["href"] if link and link.get("href") else None
                    if href and href.startswith("/"):
                        href = "https://www.myshiptracking.com" + href
                    row.append((text, href))
                rows.append(row)
            return headers, rows
    return None, []


def fetch_in_port(soup):
    headers, rows = _parse_table_by_headers(soup, ["Vessel", "Arrived"])
    vessels = []

    for row in rows:
        rowmap = dict(zip(headers, row))
        name, url = rowmap.get("Vessel", ("", None))
        if not name:
            continue
        vessels.append({
            "name": name, "url": url,
            "arrived": rowmap.get("Arrived", ("", None))[0],
            "dwt": rowmap.get("DWT", ("", None))[0],
            "grt": rowmap.get("GRT", ("", None))[0],
            "built": rowmap.get("Built", ("", None))[0],
            "size": rowmap.get("Size", ("", None))[0],
            "area": "Port Berth"
        })

    anc_headers, anc_rows = _parse_table_by_headers(soup, ["Vessel", "Area"])
    if not anc_rows:
        anc_headers, anc_rows = _parse_table_by_headers(soup, ["Vessel", "Anchored"])

    for row in anc_rows:
        rowmap = dict(zip(anc_headers, row))
        name, url = rowmap.get("Vessel", ("", None))
        if not name or any(v["name"] == name for v in vessels):
            continue
        vessels.append({
            "name": name, "url": url,
            "arrived": rowmap.get("Anchored", rowmap.get("Arrived", ("", None)))[0],
            "dwt": rowmap.get("DWT", ("", None))[0],
            "built": rowmap.get("Built", ("", None))[0],
            "area": "Anchorage Zone"
        })

    return vessels


def fetch_expected(soup):
    headers, rows = _parse_table_by_headers(soup, ["Vessel", "Estimated Arrival"])
    vessels = []
    for row in rows:
        rowmap = dict(zip(headers, row))
        name, url = rowmap.get("Vessel", ("", None))
        if not name:
            continue
        vessels.append({
            "name": name, "url": url,
            "eta": rowmap.get("Estimated Arrival", ("", None))[0],
        })
    return vessels


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return {
                    "seen": data, "update_offset": 0,
                    "last_summary_date": "", "last_expected_sent": "",
                    "last_zone_summary_date": "",
                    "pending": {}, "zone_vessels": {},
                }
            data.setdefault("update_offset", 0)
            data.setdefault("last_summary_date", "")
            data.setdefault("last_expected_sent", "")
            data.setdefault("last_zone_summary_date", "")
            data.setdefault("pending", {})
            data.setdefault("zone_vessels", {})
            return data
    return {
        "seen": [], "update_offset": 0,
        "last_summary_date": "", "last_expected_sent": "",
        "last_zone_summary_date": "",
        "pending": {}, "zone_vessels": {},
    }


def save_state(state):
    # Keep insertion order and drop the OLDEST entries once we exceed the
    # cap. Sorting alphabetically here would be wrong: it has nothing to do
    # with recency, so it could silently drop a recent event while keeping
    # an old one — which then gets re-notified if it resurfaces in the
    # site's activity table.
    seen_ordered = list(dict.fromkeys(state["seen"]))  # dedup, keep order
    state["seen"] = seen_ordered[-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram(msg, reply_markup=None):
    """Send a message to every configured chat id. A failure for one chat
    (e.g. bot blocked, bad chat id) is logged but does not stop delivery
    to the remaining chats."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for chat_id in CHAT_IDS:
        data = {"chat_id": chat_id, "text": msg}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        try:
            r = requests.post(url, data=data, timeout=30)
            r.raise_for_status()
        except requests.RequestException as exc:
            logger.error("Failed to send Telegram message to %s: %s", chat_id, exc)


def send_telegram_reply(chat_id, msg):
    """Send a message to a single chat (used to reply to a command), as
    opposed to send_telegram() which broadcasts to every configured chat."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": chat_id, "text": msg}, timeout=30)
        r.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Failed to send Telegram reply to %s: %s", chat_id, exc)


def fetch_telegram_updates(offset):
    """Poll Telegram for any messages sent to the bot since `offset`.
    timeout=0 makes this a quick, non-blocking call (we don't need
    long-polling since this whole script re-runs every 15 minutes anyway)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"offset": offset, "timeout": 0}
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            logger.warning("Telegram getUpdates returned not-ok response: %s", data)
            return []
        return data.get("result", [])
    except requests.RequestException as exc:
        logger.error("Failed to fetch Telegram updates: %s", exc)
        return []


def berth_label(vessel_details):
    lat, lon = vessel_details.get("lat"), vessel_details.get("lon")
    if lat is None or lon is None:
        return None
    berth, _dist = nearest_berth(lat, lon)
    return f"Berth {berth}" if berth else None


def format_vessel_line(v):
    extra = []
    if v.get("area"):
        extra.append(v["area"])
    if v.get("berth"):
        extra.append(v["berth"])
    if v.get("type"):
        extra.append(v["type"])
    if v.get("dwt"):
        extra.append(f"cargo: {v['dwt']}")
    extra_txt = f" ({', '.join(extra)})" if extra else ""
    arrived_txt = f" — arrived: {v['arrived']}" if v.get("arrived") else ""
    return f"🚢 {v['name']}{arrived_txt}{extra_txt}"


def maybe_send_daily_summary(soup, state):
    now = datetime.now(TZ)
    today_str = now.strftime("%Y-%m-%d")
    if now.hour < DAILY_SUMMARY_HOUR or state.get("last_summary_date") == today_str:
        return

    in_port = fetch_in_port(soup)
    expected = fetch_expected(soup)

    lines = [f"📋 Daily Summary - Port of Tartous ({today_str})\n"]
    lines.append(f"⚓ Vessels currently in port/anchorage: {len(in_port)}")
    lines.append(f"🕒 Vessels expected to arrive: {len(expected)}\n")

    if in_port:
        lines.append("Vessels in port:")
        for v in in_port:
            details = fetch_vessel_details(v.get("url"))
            v["type"] = details["type"]
            v["berth"] = berth_label(details)
            lines.append(format_vessel_line(v))

    send_telegram("\n".join(lines))
    state["last_summary_date"] = today_str


def build_expected_message(soup, header="🕒 Expected Arrivals"):
    """Format the current expected-arrivals list. Shared by the scheduled
    broadcast (maybe_send_expected_update) and the on-demand /expected
    command (handle_commands) so both stay in sync."""
    now = datetime.now(TZ)
    vessels = fetch_expected(soup)
    lines = [f"{header} - Port of Tartous ({now.strftime('%Y-%m-%d %H:%M')})\n"]
    if not vessels:
        lines.append("لا يوجد سفن متوقع وصولها حالياً.")
    else:
        for v in vessels:
            details = fetch_vessel_details(v.get("url"))
            type_txt = f" ({details['type']})" if details["type"] else ""
            eta_txt = f" — ETA: {v['eta']}" if v.get("eta") else ""
            lines.append(f"🚢 {v['name']}{type_txt}{eta_txt}")
    return "\n".join(lines)


def maybe_send_expected_update(soup, state):
    now = datetime.now(TZ)
    last_sent = state.get("last_expected_sent") or ""
    if last_sent:
        try:
            last_dt = datetime.fromisoformat(last_sent)
            if now - last_dt < EXPECTED_UPDATE_INTERVAL:
                return
        except ValueError:
            pass

    send_telegram(build_expected_message(soup))
    state["last_expected_sent"] = now.isoformat()


def gather_tracked_vessels(soup):
    """Collect a de-duplicated list of (vessel_id, name, url) from every
    table the site exposes: recent activity, vessels in port/anchorage, and
    expected arrivals. This is the closest approximation to 'every vessel
    currently near the port' available to us, since the site doesn't expose
    a raw live-AIS feed for an arbitrary area."""
    candidates = []
    for e in fetch_activity(soup):
        candidates.append((e.get("vessel"), e.get("url")))
    for v in fetch_in_port(soup):
        candidates.append((v.get("name"), v.get("url")))
    for v in fetch_expected(soup):
        candidates.append((v.get("name"), v.get("url")))

    seen_ids = set()
    result = []
    for name, url in candidates:
        if not url:
            continue
        vid = extract_vessel_id(url) or name
        if not vid or vid in seen_ids:
            continue
        seen_ids.add(vid)
        result.append((vid, name, url))
    return result


def check_zone_entries(soup, state, zone=ZONE_A, zone_label=ZONE_A_LABEL):
    """Notify once per entry when a tracked vessel's latest reported
    position falls inside `zone`. Uses state['zone_vessels'] as a dict of
    vessel_id -> {name, url, entered_at}, so the same vessel doesn't trigger
    a new alert every run while it stays there — only on entry. When it
    later reports a position outside the zone, it's cleared, so a future
    re-entry will alert again. The stored name/url/entered_at is also reused
    by maybe_send_zone_daily_summary() to list who's currently inside."""
    zone_state = state.setdefault("zone_vessels", {})
    currently_inside = set()
    now_iso = datetime.now(TZ).isoformat()

    for vid, name, url in gather_tracked_vessels(soup):
        details = fetch_vessel_details(url)
        if details["lat"] is None or details["lon"] is None:
            continue
        if point_in_zone(details["lat"], details["lon"], zone):
            currently_inside.add(vid)
            if vid not in zone_state:
                berth = berth_label(details)
                berth_txt = f"\n⚓ {berth}" if berth else ""
                type_txt = f" ({details['type']})" if details["type"] else ""
                msg = (
                    f"🟡 دخول سفينة إلى {zone_label}\n"
                    f"🚢 {name}{type_txt}\n"
                    f"📍 {details['lat']}, {details['lon']} "
                    f"(آخر تحديث: {details.get('reported') or '—'}){berth_txt}"
                )
                send_telegram(msg)
                zone_state[vid] = {"name": name, "url": url, "entered_at": now_iso}

    # Vessels that were inside before but aren't anymore have left the zone;
    # drop them so a later re-entry can alert again.
    for vid in list(zone_state.keys()):
        if vid not in currently_inside:
            zone_state.pop(vid, None)


def build_zone_summary_message(state, zone_label=ZONE_A_LABEL, header="🧭 السفن الواصلة إلى"):
    """Format the current Yatur-zone table. Shared by the scheduled daily
    digest (maybe_send_zone_daily_summary) and the on-demand /yatur command
    (handle_commands) so both stay in sync."""
    now = datetime.now(TZ)
    today_str = now.strftime("%Y-%m-%d %H:%M")
    zone_state = state.get("zone_vessels", {})
    lines = [f"{header} {zone_label} ({today_str})\n"]

    if not zone_state:
        lines.append(f"لا يوجد سفن حالياً بمنطقة {zone_label}.")
    else:
        for vid, info in zone_state.items():
            name = info.get("name") or vid
            entered_at = info.get("entered_at")
            entered_txt = ""
            if entered_at:
                try:
                    entered_txt = f" — دخلت: {datetime.fromisoformat(entered_at).strftime('%Y-%m-%d %H:%M')}"
                except ValueError:
                    pass
            details = fetch_vessel_details(info.get("url"))
            berth = berth_label(details)
            berth_txt = f" ({berth})" if berth else ""
            lines.append(f"🚢 {name}{berth_txt}{entered_txt}")

    return "\n".join(lines)


def maybe_send_zone_daily_summary(state, zone_label=ZONE_A_LABEL):
    """Send one daily digest listing every vessel currently considered
    'inside' the watch zone (state['zone_vessels']), titled after the zone.
    Runs once a day at ZONE_DAILY_SUMMARY_HOUR (7 AM Damascus time)."""
    now = datetime.now(TZ)
    today_str = now.strftime("%Y-%m-%d")
    if now.hour < ZONE_DAILY_SUMMARY_HOUR or state.get("last_zone_summary_date") == today_str:
        return

    send_telegram(build_zone_summary_message(state, zone_label))
    state["last_zone_summary_date"] = today_str


def handle_commands(soup, state):
    """Check for any new Telegram messages since the last run and reply to
    recognized commands. Only messages coming from a chat id already in
    CHAT_IDS are honored, so a random person who finds the bot can't trigger
    it. Because this whole script only runs every 15 minutes, replies are
    not instant — they go out on the next scheduled run after the command
    was sent."""
    offset = state.get("update_offset", 0)
    updates = fetch_telegram_updates(offset)
    if not updates:
        return

    highest_id = offset - 1
    for update in updates:
        update_id = update.get("update_id")
        if update_id is not None and update_id > highest_id:
            highest_id = update_id

        message = update.get("message") or update.get("channel_post")
        if not message:
            continue

        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            continue

        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id not in CHAT_IDS:
            logger.warning("Ignoring command from unrecognized chat_id %s", chat_id)
            continue

        # Strip a "@BotUsername" suffix, which Telegram adds automatically
        # for commands sent in group chats.
        command = text.split()[0].split("@")[0].lower()

        if command == "/expected":
            reply = build_expected_message(soup, header="🕒 Expected Arrivals (on demand)")
            send_telegram_reply(chat_id, reply)
        elif command == "/yatur":
            reply = build_zone_summary_message(state)
            send_telegram_reply(chat_id, reply)
        else:
            logger.info("Received unrecognized command: %s", command)

    if highest_id >= offset:
        state["update_offset"] = highest_id + 1


def page_looks_valid(soup):
    if soup is None:
        return False
    text = soup.get_text()
    if "No Internet" in text and "Vessels In Port" not in text:
        return False
    return True


# How many consecutive "seen once, not confirmed" cycles we tolerate before
# giving up on an event and treating it as seen anyway (so it doesn't loop
# forever waiting for a confirmation that will never come).
MAX_PENDING_RETRIES = 3


def main():
    soup = fetch_page()
    if not page_looks_valid(soup):
        logger.warning("Page did not look valid on this run; skipping.")
        return

    state = load_state()
    handle_commands(soup, state)
    # Use a dict as an ordered set: preserves the real order keys were
    # first seen in, so pruning later (save_state) drops the oldest ones
    # instead of an arbitrary/alphabetical selection.
    seen = dict.fromkeys(state.get("seen", []))
    pending = state.get("pending", {})
    first_run = len(seen) == 0

    events = fetch_activity(soup)
    new_events = [e for e in events if e["key"] not in seen]

    if new_events and not first_run:
        soup2 = fetch_page()
        if page_looks_valid(soup2):
            events2 = {e["key"] for e in fetch_activity(soup2)}
            for e in new_events:
                if e["key"] in events2:
                    # Confirmed on the second pass: send notification.
                    details = fetch_vessel_details(e.get("url"))
                    type_txt = f" ({details['type']})" if details["type"] else ""
                    berth = berth_label(details)
                    berth_txt = f"\n⚓ {berth}" if berth else ""
                    icon = "🟢 ARRIVAL" if e["event"] == "ARRIVAL" else "🔴 DEPARTURE"
                    msg = f"{icon}: 🚢 {e['vessel']}{type_txt}\n⏱ {e['time']}{berth_txt}"
                    send_telegram(msg)
                    seen[e["key"]] = None
                    pending.pop(e["key"], None)
                else:
                    # Not confirmed this time. Track how many times this has
                    # happened; if it keeps failing to confirm, drop it into
                    # "seen" anyway so it doesn't resurface and get sent as a
                    # (possibly stale/duplicate) notification later.
                    attempts = pending.get(e["key"], 0) + 1
                    if attempts >= MAX_PENDING_RETRIES:
                        logger.warning(
                            "Event %s never confirmed after %d attempts; "
                            "marking as seen without notifying.",
                            e["key"], attempts,
                        )
                        seen[e["key"]] = None
                        pending.pop(e["key"], None)
                    else:
                        pending[e["key"]] = attempts
        else:
            logger.warning("Second-pass fetch failed; will retry unconfirmed events next run.")

    if first_run:
        for e in events:
            seen[e["key"]] = None

    state["seen"] = list(seen)
    state["pending"] = pending

    check_zone_entries(soup, state)

    maybe_send_daily_summary(soup, state)
    maybe_send_expected_update(soup, state)
    maybe_send_zone_daily_summary(state)

    save_state(state)


if __name__ == "__main__":
    main()
