"""Print the owner's Apple Calendar (iCloud CalDAV) events for Atlas.

Usage: python calendar_events.py [today|tomorrow|week|YYYY-MM-DD [YYYY-MM-DD]]
Credentials: APPLE_ID and APPLE_APP_PASSWORD in .env - an app-specific
password from account.apple.com (Sign-In & Security -> App-Specific Passwords).
"""

import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env", override=True)  # the bot's stale env copy must not beat a freshly rotated password
sys.path.insert(0, str(BASE.parent / "Shared"))
import owner  # noqa: E402

APPLE_ID = os.getenv("APPLE_ID", "").strip() or owner.get("APPLE_ID") or owner.EMAIL
APP_PASSWORD = (os.getenv("APPLE_APP_PASSWORD", "").strip() or owner.get("APPLE_APP_PASSWORD")).replace(" ", "")


def parse_args(argv: list[str]) -> tuple[date, date]:
    today = datetime.now(owner.TZ).date()
    if not argv or argv[0] == "today":
        return today, today
    if argv[0] == "tomorrow":
        d = today + timedelta(days=1)
        return d, d
    if argv[0] == "week":
        return today, today + timedelta(days=7)
    start = date.fromisoformat(argv[0])
    end = date.fromisoformat(argv[1]) if len(argv) > 1 else start
    return start, end


def local_zone():
    """The owner's time zone (see calendar_add.local_zone)."""
    return owner.TZ


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # event titles vs cp1252 consoles
    if not APPLE_ID or not APP_PASSWORD:
        sys.exit(
            "Calendar not connected: APPLE_ID / APPLE_APP_PASSWORD missing from .env.\n"
            f"{owner.NAME} makes one at account.apple.com -> Sign-In & Security -> "
            "App-Specific Passwords, then texts it to Atlas or pastes it in the "
            "Control Center Settings tab."
        )
    start_d, end_d = parse_args(sys.argv[1:])

    import caldav  # imported late: keeps --help-style usage instant

    client = caldav.DAVClient(
        url="https://caldav.icloud.com/", username=APPLE_ID, password=APP_PASSWORD
    )
    zone = local_zone()
    start = datetime.combine(start_d, time.min, tzinfo=zone)
    end = datetime.combine(end_d + timedelta(days=1), time.min, tzinfo=zone)

    rows = []
    for cal in client.principal().calendars():
        try:
            cal_name = cal.get_display_name()
        except Exception:  # noqa: BLE001
            cal_name = "Calendar"
        try:
            events = cal.search(start=start, end=end, event=True, expand=True)
        except Exception:  # noqa: BLE001 - some servers reject expand; retry plain
            events = cal.search(start=start, end=end, event=True)
        for ev in events:
            comp = ev.icalendar_component
            summary = str(comp.get("SUMMARY", "(no title)"))
            dtstart = comp.get("DTSTART").dt
            dtend_prop = comp.get("DTEND")
            if isinstance(dtstart, datetime):
                # A time with no zone at all ("floating") means wall-clock time here.
                local = dtstart.astimezone(zone) if dtstart.tzinfo else dtstart.replace(tzinfo=zone)
                clock = lambda d: d.strftime("%I:%M%p").lstrip("0").lower()  # noqa: E731
                when = f"{local.strftime('%a %b')} {local.day} {clock(local)}"
                if dtend_prop is not None and isinstance(dtend_prop.dt, datetime):
                    dtend = dtend_prop.dt
                    local_end = dtend.astimezone(zone) if dtend.tzinfo else dtend.replace(tzinfo=zone)
                    when += "-" + clock(local_end)
                    # A day or more long: say which day it ends, or a Friday-to-Sunday
                    # tournament reads as Friday only. It goes after the title so the
                    # "day time-time |" shape other code matches on stays the same.
                    if local_end - local >= timedelta(days=1):
                        summary += f" (ends {local_end.strftime('%a %b')} {local_end.day})"
                sort_key = local
            else:  # all-day events arrive as plain dates
                when = f"{dtstart.strftime('%a %b')} {dtstart.day}"
                if dtend_prop is not None and not isinstance(dtend_prop.dt, datetime):
                    last_day = dtend_prop.dt - timedelta(days=1)  # DTEND is exclusive
                    if last_day > dtstart:
                        when += f"-{last_day.strftime('%b')} {last_day.day}"
                when += " all day"
                sort_key = datetime.combine(dtstart, time.min, tzinfo=zone)
            rows.append((sort_key, f"{when} | {summary} [{cal_name}]"))

    rows.sort(key=lambda r: r[0])
    if not rows:
        print(f"No events {start_d} to {end_d}.")
        return
    for _, line in rows:
        print(line)


if __name__ == "__main__":
    main()
