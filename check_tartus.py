import json
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
EXPECTED_UPDATE_INTERVAL = timedelta(hours=3)

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
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def fetch_activity(soup):
    events = []
    today_date = datetime.now(TZ).strftime("%Y-%m-%d")
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
        # المفتاح المحمي بالرقم والتاريخ لضمان التحديثات للرحلات المستقبليّة
        key = f"{today_date}|{v_id}|{event_txt}"

        events.append({
            "key": key, "time": time_txt, "event": event_txt,
            "vessel": vessel_txt, "url": vessel_url,
        })
    return events


def fetch_vessel_type(vessel_url):
    if not vessel_url:
        return None
    try:
        resp = requests.get(vessel_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        vsoup = BeautifulSoup(resp.text, "html.parser")
        h2 = vsoup.find("h2")
        if h2:
            type_txt = h2.get_text(strip=True)
            if type_txt:
                return type_txt
    except requests.RequestException:
        pass
    return None


def fetch_vessel_position(vessel_url):
    if not vessel_url:
        return None
    try:
        resp = requests.get(vessel_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        vsoup = BeautifulSoup(resp.text, "html.parser")

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
                            return {"lat": lat, "lon": lon, "reported": time_txt}
                break

        text = vsoup.get_text(" ", strip=True)
        match = re.search(
            r"coordinates\s+(-?\d+\.\d+)\s*[°]?\s*/\s*(-?\d+\.\d+)\s*[°]?\s*"
            r"as reported on\s+([0-9:\-\s]+?)\s+by AIS",
            text,
        )
        if match:
            lat, lon, reported = match.groups()
            return {"lat": lat, "lon": lon, "reported": reported.strip()}
    except requests.RequestException:
        pass
    return None


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
                }
            data.setdefault("update_offset", 0)
            data.setdefault("last_summary_date", "")
            data.setdefault("last_expected_sent", "")
            return data
    return {
        "seen": [], "update_offset": 0,
        "last_summary_date": "", "last_expected_sent": "",
    }


def save_state(state):
    state["seen"] = sorted(set(state["seen"]))[-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for chat_id in CHAT_IDS:
        r = requests.post(url, data={"chat_id": chat_id, "text": msg}, timeout=30)
        r.raise_for_status()


def berth_label(vessel_url):
    pos = fetch_vessel_position(vessel_url)
    if not pos:
        return None
    berth, _dist = nearest_berth(pos["lat"], pos["lon"])
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
            v["type"] = fetch_vessel_type(v.get("url"))
            v["berth"] = berth_label(v.get("url"))
            lines.append(format_vessel_line(v))

    send_telegram("\n".join(lines))
    state["last_summary_date"] = today_str


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

    vessels = fetch_expected(soup)
    lines = [f"🕒 Expected Arrivals update - Port of Tartous ({now.strftime('%Y-%m-%d %H:%M')})\n"]
    if not vessels:
        lines.append("No expected arrivals currently listed.")
    else:
        for v in vessels:
            vtype = fetch_vessel_type(v.get("url"))
            type_txt = f" ({vtype})" if vtype else ""
            eta_txt = f" — ETA: {v['eta']}" if v.get("eta") else ""
            lines.append(f"🚢 {v['name']}{type_txt}{eta_txt}")

    send_telegram("\n".join(lines))
    state["last_expected_sent"] = now.isoformat()


def page_looks_valid(soup):
    text = soup.get_text()
    if "No Internet" in text and "Vessels In Port" not in text:
        return False
    return True


def main():
    soup = fetch_page()
    if not page_looks_valid(soup):
        return

    state = load_state()
    seen = set(state.get("seen", []))
    first_run = len(seen) == 0

    events = fetch_activity(soup)
    new_events = [e for e in events if e["key"] not in seen]

    if new_events and not first_run:
        soup2 = fetch_page()
        if page_looks_valid(soup2):
            events2 = {e["key"] for e in fetch_activity(soup2)}
            for e in new_events:
                if e["key"] in events2:
                    vtype = fetch_vessel_type(e.get("url"))
                    type_txt = f" ({vtype})" if vtype else ""
                    icon = "🟢 ARRIVAL" if e["event"] == "ARRIVAL" else "🔴 DEPARTURE"
                    msg = f"{icon}: 🚢 {e['vessel']}{type_txt}\n⏱ {e['time']}"
                    send_telegram(msg)
                    seen.add(e["key"])

    if first_run:
        for e in events:
            seen.add(e["key"])

    state["seen"] = list(seen)

    maybe_send_daily_summary(soup, state)
    maybe_send_expected_update(soup, state)

    save_state(state)


if __name__ == "__main__":
    main()
