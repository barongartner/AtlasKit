"""Print events from the owner's extra calendar feeds (ICS subscription links:
team schedules, work rosters, school timetables, anything not on iCloud).

Usage: python ics_today.py [today|tomorrow|week|YYYY-MM-DD] [YYYY-MM-DD]

Feed URLs come from the ICS_FEEDS setting, comma-separated (Shared\\.env, or
this folder's .env). They often carry a private token, so they are never
printed. These feeds are not on iCloud CalDAV, so calendar_events.py never
shows them.
"""
import os
import re
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlparse

import icalendar
import recurring_ical_events
import requests
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env", override=True)
sys.path.insert(0, str(BASE.parent / "Shared"))
import owner  # noqa: E402

TZ = owner.TZ


def feeds() -> list[tuple[str, str]]:
    """[(label, url)] from ICS_FEEDS. The label is the feed's number and host."""
    raw = owner.get("ICS_FEEDS") or os.getenv("ICS_FEEDS", "")
    urls = [u.strip().strip("<>\"'") for u in raw.split(",") if u.strip()]
    out = []
    for i, url in enumerate(urls, 1):
        if url.lower().startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        host = urlparse(url).hostname or "unknown host"
        out.append((f"Feed {i} ({host})", url))
    return out


def parse_args():
    today = datetime.now(TZ).date()
    a = sys.argv[1:] or ["today"]
    if a[0] == "week":  # same word calendar_events.py takes
        return today, today + timedelta(days=7)
    first = {"today": today, "tomorrow": today + timedelta(days=1)}.get(a[0])
    try:
        start = first or date.fromisoformat(a[0])
        end = date.fromisoformat(a[1]) if len(a) > 1 else start
    except ValueError:
        sys.exit("Usage: python ics_today.py [today|tomorrow|week|YYYY-MM-DD] [YYYY-MM-DD]")
    return start, end


def clock(d):
    return f"{d.hour % 12 or 12}:{d.minute:02d}{'am' if d.hour < 12 else 'pm'}"


def when(ev_start, ev_end):
    if not isinstance(ev_start, datetime):
        return f"{ev_start:%a %b} {ev_start.day} all day"
    s = ev_start.astimezone(TZ) if ev_start.tzinfo else ev_start.replace(tzinfo=TZ)
    out = f"{s:%a %b} {s.day} {clock(s)}"
    if isinstance(ev_end, datetime):
        e = ev_end.astimezone(TZ) if ev_end.tzinfo else ev_end.replace(tzinfo=TZ)
        out += "-" + clock(e)
    return out


def load_calendar(text: str):
    try:
        return icalendar.Calendar.from_ical(text)
    except Exception:  # noqa: BLE001
        # Some feeds ship an empty VTIMEZONE that crashes the parser; their
        # events carry a real TZID anyway, so retry without it.
        body = re.sub(r"BEGIN:VTIMEZONE.*?END:VTIMEZONE\r?\n?", "", text, flags=re.S)
        return icalendar.Calendar.from_ical(body)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    start, end = parse_args()
    feed_list = feeds()
    if not feed_list:
        print("No extra calendar feeds set up.")
        return
    lo = datetime.combine(start, time.min, TZ)
    hi = datetime.combine(end + timedelta(days=1), time.min, TZ)
    found = []
    failed = []
    for label, url in feed_list:
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            events = recurring_ical_events.of(load_calendar(r.text)).between(lo, hi)
        except Exception as e:  # noqa: BLE001
            print(f"[{label}] fetch failed: {type(e).__name__}")
            failed.append(label)
            continue
        for ev in events:
            s = ev.get("DTSTART").dt
            e = ev.get("DTEND").dt if ev.get("DTEND") else None
            if isinstance(s, datetime):
                sort = s.astimezone(TZ) if s.tzinfo else s.replace(tzinfo=TZ)
            else:
                sort = datetime.combine(s, time.min, TZ)
            summary = " ".join(str(ev.get("SUMMARY", "")).split())
            loc = " ".join(str(ev.get("LOCATION", "")).split())
            found.append((sort, label, when(s, e), summary, loc))
    for _, label, w, summary, loc in sorted(found):
        print(f"{w} | {label} | {summary}" + (f" | {loc}" if loc else ""))
    if failed:
        # A feed that couldn't be read is NOT "nothing on": say so and fail,
        # so nobody calls the day clear off half an answer.
        print(f"COULD NOT CHECK: {', '.join(failed)}. Don't say there's nothing on {start} to {end}.")
        sys.exit(1)
    if not found:
        print(f"No events in the extra feeds {start} to {end}.")


if __name__ == "__main__":
    main()
