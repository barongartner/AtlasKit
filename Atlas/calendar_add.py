"""Add an event to the owner's Apple Calendar (iCloud CalDAV).

Usage:
  python calendar_add.py "Title" 2026-09-01                      (all-day)
  python calendar_add.py "Title" 2026-09-01 7:30pm               (1 hour long)
  python calendar_add.py "Title" 2026-09-01 7:30pm 9pm
  python calendar_add.py "Title" 2026-09-01 7:30pm 9pm --location "somewhere" --notes "details"
  python calendar_add.py "Dentist" 2026-09-10 --calendar Work

Times take 12-hour (7:30pm, 9am) or 24-hour (19:30) forms.
Credentials: APPLE_ID and APPLE_APP_PASSWORD in .env (same as calendar_events.py).
Writes to the CALENDAR_NAME calendar (or the account's first event calendar
when that's blank), so it shows up on the owner's iPhone natively.
--calendar picks another one by name.
"""

import os
import re
import sys
import uuid
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env", override=True)  # the bot's stale env copy must not beat a freshly rotated password
sys.path.insert(0, str(BASE.parent / "Shared"))
import owner  # noqa: E402

APPLE_ID = os.getenv("APPLE_ID", "").strip() or owner.get("APPLE_ID") or owner.EMAIL
APP_PASSWORD = (os.getenv("APPLE_APP_PASSWORD", "").strip() or owner.get("APPLE_APP_PASSWORD")).replace(" ", "")
CALENDAR_NAME = os.getenv("CALENDAR_NAME", "").strip() or owner.get("CALENDAR_NAME")


def local_zone():
    """The owner's time zone (OWNER_TIMEZONE). Not `.astimezone()` with no
    zone: Python applies the current year's Windows rule to every year, which
    goes wrong when a zone's rules change between years."""
    return owner.TZ


def ics_text(value: str) -> str:
    """Text made safe for one iCalendar line. Raw, a line break in --notes cut
    the note off at its first line, and a second line shaped like a property
    (RRULE:..., END:VEVENT) became part of the event."""
    value = value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return re.sub(r"\r\n|\r|\n", "\\\\n", value)


def parse_time(text: str) -> time:
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text.strip().lower())
    if not m:
        sys.exit(f"Can't parse time: {text!r} (use 7:30pm, 9am, or 19:30)")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3) == "pm" and hour != 12:
        hour += 12
    elif m.group(3) == "am" and hour == 12:
        hour = 0
    return time(hour, minute)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    if not APPLE_ID or not APP_PASSWORD:
        sys.exit("Calendar not connected: APPLE_ID / APPLE_APP_PASSWORD missing from .env.")

    args = sys.argv[1:]
    location = notes = ""
    calendar_name = CALENDAR_NAME
    for flag in ("--location", "--notes", "--calendar"):
        if flag in args:
            i = args.index(flag)
            value = args[i + 1] if i + 1 < len(args) else ""
            args = args[:i] + args[i + 2:]
            if flag == "--location":
                location = value
            elif flag == "--notes":
                notes = value
            else:
                calendar_name = value
    if len(args) < 2:
        sys.exit('Usage: python calendar_add.py "Title" YYYY-MM-DD [start] [end] [--location x] [--notes x]')

    title, day = args[0], date.fromisoformat(args[1])
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    uid = uuid.uuid4()

    this_year = "" if day.year == date.today().year else f", {day.year}"
    if len(args) >= 3:  # timed event
        zone = local_zone()
        start = datetime.combine(day, parse_time(args[2]), tzinfo=zone)
        end = (
            datetime.combine(day, parse_time(args[3]), tzinfo=zone)
            if len(args) >= 4
            else start + timedelta(hours=1)
        )
        if end <= start:  # "11pm 1am" rolls past midnight
            end += timedelta(days=1)
        # UTC, not TZID=<zone>: iCloud's zone data can lag behind a rule change
        # and land the event an hour off; a UTC time is always right.
        fmt = "%Y%m%dT%H%M%SZ"
        when_lines = (
            f"DTSTART:{start.astimezone(UTC).strftime(fmt)}\n"
            f"DTEND:{end.astimezone(UTC).strftime(fmt)}"
        )
        clock = lambda d: d.strftime("%I:%M%p").lstrip("0").lower()  # noqa: E731
        when_said = f"{start.strftime('%a %b')} {start.day}{this_year} {clock(start)}-{clock(end)}"
    else:  # all-day
        when_lines = (
            f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}\n"
            f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}"
        )
        when_said = f"{day.strftime('%a %b')} {day.day}{this_year} all day"

    extra = ""
    if location:
        extra += f"LOCATION:{ics_text(location)}\n"
    if notes:
        extra += f"DESCRIPTION:{ics_text(notes)}\n"

    ical = (
        "BEGIN:VCALENDAR\n"
        "VERSION:2.0\n"
        "PRODID:-//Atlas//calendar_add//EN\n"
        "BEGIN:VEVENT\n"
        f"UID:{uid}\n"
        f"DTSTAMP:{stamp}\n"
        f"SUMMARY:{ics_text(title)}\n"
        f"{when_lines}\n"
        f"{extra}"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )

    import caldav

    client = caldav.DAVClient(
        url="https://caldav.icloud.com/", username=APPLE_ID, password=APP_PASSWORD
    )
    names = []
    for cal in client.principal().calendars():
        try:
            name = cal.get_display_name()
        except Exception:  # noqa: BLE001
            continue
        if not calendar_name:  # no name set: the first calendar that holds events
            try:
                if "VEVENT" not in cal.get_supported_components():
                    continue
            except Exception:  # noqa: BLE001 - can't tell: assume it does
                pass
        elif name != calendar_name:
            names.append(name)
            continue
        cal.save_event(ical)
        print(f"Added [{name}]: {when_said} | {title}" + (f" @ {location}" if location else ""))
        return
    if calendar_name:
        sys.exit(f'Calendar "{calendar_name}" not found on the iCloud account. '
                 f"Calendars there: {', '.join(names) or 'none'}.")
    sys.exit("No calendar on the iCloud account accepts events.")


if __name__ == "__main__":
    main()
