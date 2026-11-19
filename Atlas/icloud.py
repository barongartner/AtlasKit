"""The owner's iCloud (Reminders, Notes, Find My) for Atlas, over the iCloud web API.

Usage:
  python icloud.py login                     once; the owner types password + 2FA
  python icloud.py status                    is the session still good?
  python icloud.py reminders [list]          open reminders (all lists if none)
  python icloud.py reminders --all [list]    include completed ones
  python icloud.py lists                     reminder lists with counts
  python icloud.py newlist "Name" [--color red|orange|yellow|green|blue|purple|brown]
  python icloud.py add "Title" [YYYY-MM-DD [7:30pm]] [--list X] [--notes x] [--flag]
        [--arrive "place or address"] [--leave "place or address"] [--radius 150]
        [--repeat daily|weekly|monthly|yearly] [--url https://...]
        [--priority high|medium|low|none]   (!!!, !!, ! on the phone)
        [--under "parent title"]   makes it a subtask of that open reminder, same list
      A date+time is the time alert. --arrive/--leave add a location alert
      (geocoded with OpenStreetMap). Apple's "when messaging a person" alert
      is not in the web API, so it can't be set from here.
  python icloud.py done "title"              complete the open reminder matching title
  python icloud.py edit "title" [--title x] [--due YYYY-MM-DD [time]] [--nodue] [--notes x] [--priority p]
  python icloud.py delete "title"            only when the owner asks
  python icloud.py notes                     note titles, newest first
  python icloud.py note "title"              full text of one note
  python icloud.py search "words"            notes containing the words
  python icloud.py where                     where the owner's iPhone is right now
Title matching is case-insensitive substring; two matches = it lists both and stops.
Notes are read-only (pyicloud can't write them). The web session lives in
~\\.atlas\\icloud (the user's home folder), outside the kit so a backup of the
kit never copies it. When it expires every command says so; the owner reruns
login on the PC.
Settings: APPLE_ID (falls back to OWNER_EMAIL), REMINDERS_LIST (optional
default list for `add`), FIND_MY_DEVICE (optional exact device name for `where`).
"""

import getpass
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env", override=True)
sys.path.insert(0, str(BASE.parent / "Shared"))
import owner  # noqa: E402

from pyicloud import PyiCloudService  # noqa: E402
from pyicloud.exceptions import PyiCloudFailedLoginException  # noqa: E402
from pyicloud.services.reminders.models.domain import Proximity, RecurrenceFrequency  # noqa: E402

APPLE_ID = os.getenv("APPLE_ID", "").strip() or owner.get("APPLE_ID") or owner.EMAIL
COOKIES = Path.home() / ".atlas" / "icloud"
EXPIRED = (f"iCloud session expired. {owner.NAME} has to run this on the PC: "
           'python "' + str(BASE / "icloud.py") + '" login')


def connect(password: str | None = None) -> PyiCloudService:
    COOKIES.mkdir(parents=True, exist_ok=True)
    return PyiCloudService(APPLE_ID, password, cookie_directory=str(COOKIES))


def session() -> PyiCloudService:
    try:
        api = connect()
    except PyiCloudFailedLoginException:
        sys.exit(EXPIRED)
    if api.requires_2fa or api.requires_2sa:
        sys.exit(EXPIRED)
    return api


def login() -> None:
    # Apple locks the account after a handful of bad passwords, so stop at 4.
    for attempt in range(1, 5):
        try:
            api = connect(getpass.getpass(f"Apple ID password for {APPLE_ID} (try {attempt} of 4): "))
            break
        except PyiCloudFailedLoginException as e:
            print(f"Wrong: {e}")
    else:
        sys.exit("4 misses. Stop guessing and reset it on your iPhone before Apple locks the account.")
    if api.requires_2fa:
        code = input("2FA code from your iPhone: ").strip()
        if not api.validate_2fa_code(code):
            sys.exit("Apple rejected that code.")
        if not api.is_trusted_session:
            api.trust_session()
    print("Logged in. Session saved.")


def parse_when(args: list[str]) -> datetime | None:
    """'2026-11-05' or '2026-11-05 7:30pm' -> local datetime (date-only = 9am)."""
    if not args:
        return None
    day = datetime.strptime(args[0], "%Y-%m-%d")
    if len(args) > 1:
        t = datetime.strptime(args[1].lower().replace(" ", ""), "%I:%M%p" if ":" in args[1] else "%I%p")
        day = day.replace(hour=t.hour, minute=t.minute)
    else:
        day = day.replace(hour=9)
    return day.replace(tzinfo=local_zone())


def local_zone():
    """The owner's time zone (OWNER_TIMEZONE). Plain `.astimezone()` applies
    the current year's Windows rule to every year, which goes wrong when a
    zone's rules change between years."""
    return owner.TZ


def geocode(place: str) -> tuple[str, float, float]:
    """Address or place name -> (display name, lat, lon). Tries it in the
    owner's city (OWNER_CITY) first, then as written."""
    tries = [place]
    if owner.CITY and owner.CITY.lower() not in place.lower():
        tries.insert(0, f"{place}, {owner.CITY}")
    hits = []
    for query in tries:
        q = urllib.parse.urlencode({"q": query, "format": "json", "limit": 1})
        req = urllib.request.Request(f"https://nominatim.openstreetmap.org/search?{q}",
                                     headers={"User-Agent": "Atlas"})
        hits = json.load(urllib.request.urlopen(req, timeout=20))
        if hits:
            break
        time.sleep(1)  # Nominatim's limit is one request a second
    if not hits:
        sys.exit(f"Couldn't find '{place}' on the map. Give a fuller address.")
    name = ", ".join(hits[0]["display_name"].split(", ")[:3])
    return name, float(hits[0]["lat"]), float(hits[0]["lon"])


PRIORITY = {"high": 1, "medium": 5, "low": 9, "none": 0}  # Apple's values
PRIORITY_MARK = {1: " | !!!", 5: " | !!", 9: " | !"}


def pop_flag(args: list[str], name: str, takes: int = 1) -> list[str] | None:
    if name not in args:
        return None
    i = args.index(name)
    vals = args[i + 1:i + 1 + takes]
    del args[i:i + 1 + takes]
    return vals


def fmt_due(r) -> str:
    if not r.due_date:
        return ""
    d = r.due_date.astimezone(local_zone())
    year = "" if d.year == datetime.now().year else f", {d.year}"
    return f" | due {d:%a %b} {d.day}{year}" + ("" if r.all_day else f" {d:%I:%M %p}".replace(" 0", " "))


def find_one(items: list, title: str, label: str):
    hits = [x for x in items if title.lower() in (x.title or "").lower()]
    exact = [x for x in hits if (x.title or "").lower() == title.lower()]
    hits = exact or hits
    if len(hits) != 1:
        names = "\n".join(f"- {x.title}" for x in hits) or "none"
        sys.exit(f"{len(hits)} {label} match '{title}':\n{names}")
    return hits[0]


def default_list(lists: list):
    """Where a reminder goes when no --list is given: REMINDERS_LIST if set,
    else a list called Reminders or To Do, else the first list."""
    wanted = owner.get("REMINDERS_LIST")
    if wanted:
        return find_one(lists, wanted, "lists")
    for name in ("Reminders", "To Do"):
        exact = [l for l in lists if (l.title or "").lower() == name.lower()]
        if exact:
            return exact[0]
    if not lists:
        sys.exit("No reminder lists on the account. Make one with newlist first.")
    return lists[0]


def owner_phone(api):
    """FIND_MY_DEVICE if set, else an iPhone named after the owner, else the
    only iPhone on the account."""
    devices = list(api.devices)
    phones = [d for d in devices if "iphone" in (d.name or "").lower()]
    wanted = owner.get("FIND_MY_DEVICE")
    if wanted:
        hits = [d for d in devices if (d.name or "").lower() == wanted.lower()]
    else:
        hits = [d for d in phones if (d.name or "").lower().startswith(owner.NAME.lower())] or phones
    if len(hits) != 1:
        names = ", ".join(d.name for d in phones) or "none"
        sys.exit(f"Can't tell which iPhone is {owner.NAME}'s (iPhones on the account: {names}). "
                 "Set FIND_MY_DEVICE to the right device name.")
    return hits[0]


def open_reminders(api) -> list:
    return [r for r in api.reminders.reminders() if not r.completed and not r.deleted]


LIST_COLORS = {
    "red": ("#FF3B30", 1, 0.231, 0.188), "orange": ("#FF9500", 1, 0.584, 0),
    "yellow": ("#FFCC00", 1, 0.8, 0), "green": ("#34C759", 0.204, 0.78, 0.349),
    "blue": ("#0088FF", 0, 0.533, 1), "purple": ("#AF52DE", 0.686, 0.322, 0.871),
    "brown": ("#A2845E", 0.635, 0.518, 0.369),
}


def new_list(api, name: str, color: list[str]) -> None:
    """pyicloud has no list create, so this writes the CloudKit List record
    itself, shaped like the lists an iPhone makes."""
    import uuid
    from pyicloud.services.reminders import _writes as W
    from pyicloud.services.reminders._protocol import _generate_resolution_token_map

    if any(l.title.lower() == name.lower() for l in api.reminders.lists() if not l.deleted):
        sys.exit(f"A list called {name} already exists.")
    sym = color[0].lower() if color[0].lower() in LIST_COLORS else "purple"
    hexs, r, g, b = LIST_COLORS[sym]
    color_json = json.dumps({"alpha": 1, "red": r, "green": g, "blue": b, "colorRGBSpace": 2,
                             "ckSymbolicColorName": sym, "daSymbolicColorName": sym,
                             "daHexString": hexs})
    fields = {
        "Name": {"type": "STRING", "value": name, "isEncrypted": True},
        "Color": {"type": "STRING", "value": color_json},
        "SortingStyle": {"type": "STRING", "value": "manual"},
        "IsGroup": {"type": "INT64", "value": 0},
        "IsLinkedToAccount": {"type": "INT64", "value": 1},
        "Imported": {"type": "INT64", "value": 0},
        "Deleted": {"type": "INT64", "value": 0},
        "ReminderIDs": {"type": "STRING", "value": "[]"},
        "ShouldCategorizeGroceryItems": {"type": "INT64", "value": 0, "isEncrypted": True},
        "ShouldAutoCategorizeItems": {"type": "INT64", "value": 0, "isEncrypted": True},
        "ResolutionTokenMap": {"type": "STRING", "value": _generate_resolution_token_map(
            ["name", "color", "sortingStyle", "isGroup", "reminderIDsMergeableOrdering"])},
    }
    wr = api.reminders._writes
    op = W.CKModifyOperation(operationType="create", record=wr._write_record(
        record_name="List/" + str(uuid.uuid4()).upper(), record_type="List", fields=fields))
    resp = wr._get_raw().modify(operations=[op], zone_id=W._REMINDERS_ZONE_REQ)
    W._assert_modify_success(resp, "Create list")
    print(f"Created list {name} ({sym}).")


def remake(api, r) -> None:
    """Apply a title or notes change by making the reminder again and moving
    its subtasks over, then deleting the old one.

    pyicloud writes the text as a brand-new Apple CRDT document with no
    deletions in it, so the iPhone merges it with the text it already has
    instead of replacing it: clearing notes does nothing, and replacing them
    leaves a mash of old lines. A fresh record has no old text to merge with.
    Alerts, repeats, links and tags would be lost in a remake, so a reminder
    with any of those is refused instead."""
    extras = [name for name, ids in (("an alert", r.alarm_ids), ("a repeat", r.recurrence_rule_ids),
                                     ("a link or attachment", r.attachment_ids), ("a tag", r.hashtag_ids)) if ids]
    if extras:
        sys.exit(f"NOT CHANGED: {r.title} has {', '.join(extras)}, which a remake would lose, and "
                 f"changing its text in place doesn't stick on the iPhone. Ask {owner.NAME} to edit it on the phone.")
    kids = [c for c in api.reminders.reminders(r.list_id)
            if c.parent_reminder_id == r.id and not c.deleted]
    new = api.reminders.create(r.list_id, r.title, desc=r.desc or "", due_date=r.due_date,
                               priority=r.priority, flagged=r.flagged, all_day=r.all_day,
                               time_zone=r.time_zone, parent_reminder_id=r.parent_reminder_id)
    for c in kids:
        c.parent_reminder_id = new.id
        api.reminders.update(c)
    api.reminders.delete(r)
    # Read it back from the server before saying it worked.
    fresh = [x for x in api.reminders.reminders(r.list_id) if not x.deleted]
    got = next((x for x in fresh if x.id == new.id), None)
    moved = sum(1 for x in fresh if x.parent_reminder_id == new.id)
    if got is None or (got.desc or "") != (r.desc or "") or got.title != r.title or moved != len(kids):
        sys.exit(f"CHECK FAILED after remaking {r.title}: notes now {got.desc if got else None!r}, "
                 f"{moved} of {len(kids)} subtasks moved. Tell {owner.NAME} it may not have worked.")
    sub = f" | {moved} subtasks kept" if kids else ""
    print(f"Updated: {r.title}{fmt_due(got)}{PRIORITY_MARK.get(got.priority, '')}{sub} (checked)")


def main(argv: list[str]) -> None:
    # Reminder titles start with an emoji and a piped Windows console is
    # cp1252: printing one crashed `add` AFTER the reminder was made (so it got
    # made twice) and killed `reminders` listings.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not argv:
        sys.exit(__doc__)
    cmd, args = argv[0], argv[1:]
    if cmd == "login":
        login()
        return
    api = session()

    if cmd == "status":
        print(f"iCloud session good for {APPLE_ID}.")
    elif cmd == "lists":
        for l in api.reminders.lists():
            if not l.deleted:
                print(f"{l.title} ({l.count})")
    elif cmd == "newlist":
        new_list(api, args[0], pop_flag(args, "--color") or ["purple"])
    elif cmd == "reminders":
        show_all = pop_flag(args, "--all", 0) is not None
        lists = [l for l in api.reminders.lists() if not l.deleted]
        if args:
            lists = [find_one(lists, args[0], "lists")]
        for l in lists:
            print(f"## {l.title}")
            rs = [r for r in api.reminders.reminders(l.id)
                  if not r.deleted and (show_all or not r.completed)]
            ids = {r.id for r in rs}
            for r in rs:
                if r.parent_reminder_id in ids:
                    continue
                for depth, x in [(0, r)] + [(1, c) for c in rs if c.parent_reminder_id == r.id]:
                    mark = "[x] " if x.completed else ""
                    print(f"{'  ' * depth}- {mark}{x.title}{fmt_due(x)}"
                          f"{PRIORITY_MARK.get(x.priority, '')}{' | flagged' if x.flagged else ''}")
    elif cmd == "add":
        lst = pop_flag(args, "--list")
        notes = pop_flag(args, "--notes")
        flag = pop_flag(args, "--flag", 0) is not None
        arrive, leave = pop_flag(args, "--arrive"), pop_flag(args, "--leave")
        radius = pop_flag(args, "--radius")
        repeat = pop_flag(args, "--repeat")
        url = pop_flag(args, "--url")
        prio = pop_flag(args, "--priority")
        under = pop_flag(args, "--under")
        lists = [l for l in api.reminders.lists() if not l.deleted]
        parent = find_one(open_reminders(api), under[0], "open reminders") if under else None
        if parent:
            target = next(l for l in lists if l.id == parent.list_id)
        else:
            target = find_one(lists, lst[0], "lists") if lst else default_list(lists)
        due = parse_when(args[1:])
        r = api.reminders.create(target.id, args[0], desc=notes[0] if notes else "",
                                 due_date=due, flagged=flag,
                                 priority=PRIORITY[prio[0].lower()] if prio else 0,
                                 time_zone=owner.TZ_NAME if due else None,
                                 parent_reminder_id=parent.id if parent else None)
        extras = []
        place = arrive or leave
        if place:
            name, lat, lon = geocode(place[0])
            api.reminders.add_location_trigger(
                r, title=name, address=name, latitude=lat, longitude=lon,
                radius=float(radius[0]) if radius else 150.0,
                proximity=Proximity.ARRIVING if arrive else Proximity.LEAVING)
            extras.append(f"{'arriving at' if arrive else 'leaving'} {name}")
        if repeat:
            api.reminders.create_recurrence_rule(
                r, frequency=RecurrenceFrequency[repeat[0].upper()])
            extras.append(f"repeats {repeat[0]}")
        if url:
            api.reminders.create_url_attachment(r, url[0])
            extras.append("link attached")
        more = f" | {', '.join(extras)}" if extras else ""
        print(f"Added to {target.title}: {r.title}{fmt_due(r)}{PRIORITY_MARK.get(r.priority, '')}{more}")
    elif cmd in ("done", "delete"):
        pool = open_reminders(api) if cmd == "done" else [
            r for r in api.reminders.reminders() if not r.deleted]
        r = find_one(pool, args[0], "open reminders" if cmd == "done" else "reminders")
        if cmd == "done":
            r.completed = True
            r.completed_date = datetime.now().astimezone()
            api.reminders.update(r)
            print(f"Completed: {r.title}")
        else:
            api.reminders.delete(r)
            print(f"Deleted: {r.title}")
    elif cmd == "edit":
        new_title = pop_flag(args, "--title")
        notes = pop_flag(args, "--notes")
        nodue = pop_flag(args, "--nodue", 0) is not None
        prio = pop_flag(args, "--priority")
        due = None
        if "--due" in args:
            i = args.index("--due")
            parts = args[i + 1:]
            take = 2 if len(parts) > 1 and not parts[1].startswith("--") else 1
            due = parse_when(parts[:take])
            del args[i:i + 1 + take]
        r = find_one(open_reminders(api), args[0], "open reminders")
        old_title, old_desc = r.title, r.desc or ""
        if new_title:
            r.title = new_title[0]
        if notes is not None:  # --notes "" means clear them
            r.desc = notes[0] if notes else ""
        if due:
            r.due_date, r.time_zone, r.all_day = due, owner.TZ_NAME, False
        if nodue:
            r.due_date = None
        if prio:
            r.priority = PRIORITY[prio[0].lower()]
        if r.title == old_title and (r.desc or "") == old_desc:
            api.reminders.update(r)
            print(f"Updated: {r.title}{fmt_due(r)}{PRIORITY_MARK.get(r.priority, '')}")
        else:
            remake(api, r)
    elif cmd == "notes":
        for s in api.notes.recents(limit=200):
            if not s.is_deleted:
                when = f" | {s.modified_at.astimezone():%b %d}" if s.modified_at else ""
                print(f"- {s.title}{when}")
    elif cmd == "note":
        s = find_one([x for x in api.notes.recents(limit=200) if not x.is_deleted], args[0], "notes")
        print(api.notes.get(s.id).text)
    elif cmd == "search":
        words = args[0].lower()
        for s in api.notes.recents(limit=200):
            if s.is_deleted:
                continue
            text = api.notes.get(s.id).text or ""
            if words in text.lower():
                i = text.lower().index(words)
                print(f"- {s.title}: ...{text[max(0, i - 60):i + 100]}...".replace("\n", " "))
    elif cmd == "where":
        # Only the owner's own phone: the account can also see family devices.
        phone = owner_phone(api)
        loc = None
        for _ in range(6):  # Find My fills the location in a few seconds after a locate
            api.devices.refresh(locate=True)
            loc = phone.location
            if loc:
                break
            time.sleep(5)
        if not loc:
            sys.exit("Find My has no location for the iPhone right now.")
        ts = datetime.fromtimestamp(loc["timeStamp"] / 1000).strftime("%I:%M %p").lstrip("0")
        print(f"{loc['latitude']}, {loc['longitude']} (accurate to {round(loc.get('horizontalAccuracy', 0))}m, as of {ts})")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
