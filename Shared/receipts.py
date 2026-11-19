r"""Did they read it? Read receipts for what the crew texted someone.

  python receipts.py Sam                a name (contacts, nickname, or someone
                                        Iris has talked to) or a number
  python receipts.py 555-123-4567
  python receipts.py group Jordan       the group chat(s) with that person in it:
                                        who read each text, and when

Lists the last few texts the crew sent in that chat and whether each one was
read, and when. Read receipts only show up if that person has them turned on,
so "not read" can also mean "read, receipts off".
The owner's own reads are left out. "Delivered" isn't passed through by the bridge.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import owner  # noqa: E402
from imessage import BRIDGE_STATE_KEY, decode_ghost, normalize_handle, split_portal_id  # noqa: E402

HS = (owner.get("MATRIX_URL") or "http://127.0.0.1:8008").rstrip("/")
KNOWN = HERE.parent / "Iris" / "Memories" / "People" / "Known.json"


def env_value(key: str) -> str:
    for env in (Path.cwd() / ".env", HERE.parent / "Iris" / ".env", HERE.parent / "Atlas" / ".env"):
        try:
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith(key + "=") and line.split("=", 1)[1].strip():
                    return line.split("=", 1)[1].strip()
        except OSError:
            continue
    return owner.get(key)  # Shared\.env, where setup writes the shared settings


def is_crew(handle: str) -> bool:
    """One of the bots' own addresses: the bot Apple ID or a +alias of it."""
    user, _, domain = owner.BOT_APPLE_ID.lower().partition("@")
    a_user, _, a_domain = handle.split(":", 1)[-1].lower().partition("@")
    return bool(user) and a_domain == domain and a_user.split("+", 1)[0] == user


def token() -> str:
    return env_value("MATRIX_TOKEN") or sys.exit("No MATRIX_TOKEN found")


def names_by_handle() -> dict[str, str]:
    """handle -> what the owner calls them (Iris's list plus their contacts)."""
    out: dict[str, str] = {}
    try:
        out.update(json.loads(KNOWN.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    try:
        import contacts
        for c in (contacts.parse(c) for c in contacts.fetch_cards()):
            name = c["nickname"] or c["name"]
            for _, v in c["phones"] + c["emails"]:
                h = normalize_handle(v)
                if h:
                    out.setdefault(h, name)
                    out[h + "|full"] = c["name"]
    except BaseException:  # contacts.py exits when iCloud creds are missing
        pass
    return out


def wanted_handles(query: str, names: dict[str, str]) -> set[str]:
    digits = re.sub(r"\D", "", query)
    if len(digits) >= 7 and not re.search(r"[A-Za-z]", query):
        return {normalize_handle(query)}
    q = query.strip().lower()
    hits = set()
    for key, name in names.items():
        handle = key.split("|")[0]
        if q == name.lower() or q in name.lower().split() or q in name.lower():
            hits.add(handle)
    return hits


def clock(ts_ms: int) -> str:
    t = datetime.fromtimestamp(ts_ms / 1000)
    day = "" if t.date() == datetime.now().date() else f"{t:%a %b} {t.day}, "
    return day + t.strftime("%I:%M %p").lstrip("0")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    group = args[0].lower() == "group"
    query = " ".join(args[1:] if group else args)
    http = httpx.Client(headers={"Authorization": f"Bearer {token()}"}, timeout=60)
    me = http.get(f"{HS}/_matrix/client/v3/account/whoami").json()["user_id"]
    owners = {normalize_handle(h) or h.strip().lower()
              for h in (env_value("IMESSAGE_OWNER") or owner.HANDLES).split(",") if h.strip()}
    names = names_by_handle()
    targets = wanted_handles(query, names)
    if not targets:
        sys.exit(f"I don't know who {query} is.")

    rooms = http.get(f"{HS}/_matrix/client/v3/joined_rooms").json().get("joined_rooms", [])
    picked = []  # (room, label): someone can have a chat per number and email
    for room in rooms:
        r = http.get(f"{HS}/_matrix/client/v3/rooms/{quote(room)}/state/m.bridge/"
                     + quote(BRIDGE_STATE_KEY, safe=""))
        if r.status_code >= 400:
            continue
        pid = (r.json().get("channel") or {}).get("id", "")
        is_group = "," in pid or pid.startswith("gid:")
        if not pid or is_group != group:
            continue
        if group:
            # Invited counts too: a member's ghost only joins once they've posted.
            events = http.get(f"{HS}/_matrix/client/v3/rooms/{quote(room)}/members").json().get("chunk", [])
            handles = [decode_ghost(ev["state_key"]) for ev in events
                       if ev.get("state_key", "").startswith("@imessage_")
                       and ev.get("content", {}).get("membership") in ("join", "invite")
                       and not is_crew(decode_ghost(ev["state_key"]))]  # the crew's own addresses
            # The bridge names a group after its members' numbers; someone who
            # hasn't posted yet is only there.
            name = http.get(f"{HS}/_matrix/client/v3/rooms/{quote(room)}/state/m.room.name").json().get("name", "")
            for part in name.split(","):
                h = normalize_handle(part)
                if h and h not in handles:
                    handles.append(h)
            handles = [h for h in handles if h.lower() not in owners]
            if set(handles) & targets:
                who = [names.get(h, h) for h in handles if h.lower() not in owners]
                picked.append((room, "Group with " + ", ".join(who)))
        else:
            handle = normalize_handle(split_portal_id(pid)[0])
            if handle in targets:
                kind = "email" if handle.startswith("mailto:") else "number"
                picked.append((room, f"Their {kind} ({handle.split(':', 1)[1]})"))
    if not picked:
        sys.exit(f"No {'group ' if group else ''}chat with {query} yet.")

    for room, label in picked:
        if len(picked) > 1 or group:
            print(label + ":")
        flt = json.dumps({"room": {"rooms": [room], "timeline": {"limit": 40, "types": ["m.room.message"]},
                                   "ephemeral": {"types": ["m.receipt"]}, "state": {"types": []}},
                          "presence": {"types": []}, "account_data": {"types": []}})
        data = http.get(f"{HS}/_matrix/client/v3/sync", params={"timeout": "0", "filter": flt}).json()
        d = data.get("rooms", {}).get("join", {}).get(room, {})
        events = d.get("timeline", {}).get("events", [])
        order = {ev["event_id"]: i for i, ev in enumerate(events)}
        # Each person's latest read marker: everything up to it is read.
        read_upto: dict[str, tuple[int, int]] = {}
        for ev in d.get("ephemeral", {}).get("events", []):
            for event_id, kinds in ev.get("content", {}).items():
                for user, info in (kinds.get("m.read") or {}).items():
                    if user == me or not user.startswith("@imessage_") or decode_ghost(user).lower() in owners:
                        continue
                    pos = order.get(event_id, -1)
                    if pos > read_upto.get(user, (-2, 0))[0]:
                        read_upto[user] = (pos, info.get("ts", 0))
        ours = [(i, ev) for i, ev in enumerate(events) if ev.get("sender") == me][-6:]
        if not ours:
            print("Nothing sent in that chat yet.")
            continue
        for i, ev in ours:
            c = ev.get("content", {})
            text = c.get("body", "") if c.get("msgtype") == "m.text" else f"[{c.get('msgtype', 'm.file')[2:]}] {c.get('body', '')}"
            readers = sorted(((ts, names.get(decode_ghost(u), decode_ghost(u)))
                              for u, (pos, ts) in read_upto.items() if pos >= i))
            when = clock(ev.get("origin_server_ts", 0))
            if group:
                status = ("read by " + ", ".join(f"{n} at {clock(ts)}" for ts, n in readers)
                          if readers else "nobody's read it yet (or their read receipts are off)")
            else:
                status = f"read at {clock(readers[0][0])}" if readers else "not read yet (or their read receipts are off)"
            print(f"{when}: {text[:70]!r} - {status}")


if __name__ == "__main__":
    main()
