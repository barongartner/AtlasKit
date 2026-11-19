r"""Drive times with live traffic for Atlas (TomTom), always with an Apple Maps link.

Usage:
  python commute.py time FROM TO                       leaving now
  python commute.py time FROM TO --leave 8:30am [--date tomorrow|YYYY-MM-DD]
  python commute.py time FROM TO --arrive 9am [--date ...]   when to walk out the door
  python commute.py link FROM TO                       just the Apple Maps directions link (no key needed)
  python commute.py places                             the saved places
  python commute.py save NAME "address or place"       remember a place (home, work, school, or any name)
  python commute.py forget NAME                        drop a saved place (not home, work or school)
  python commute.py test                               check the key

FROM and TO are a saved place name (home, work, gym...) or any address or
place ("Costco", "123 Main St"). Places are looked up near the owner's city.
A trip to a saved place uses its address as written for the Maps link.
Settings (Shared\.env): TOMTOM_KEY, COMMUTE_HOME, COMMUTE_WORK, COMMUTE_SCHOOL,
and COMMUTE_PLACES ("gym=123 Main St; rink=Arena name").
"""

import re
import sys
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Shared"))
import addons  # noqa: E402
import owner  # noqa: E402

TT = "https://api.tomtom.com"
FIXED = ("home", "work", "school")
MILES = {"US", "GB", "LR", "MM"}  # countries that drive in miles
_country: dict = {}


class CommuteError(Exception):
    pass


# ---------------------------------------------------------------- places --

def places() -> dict[str, str]:
    out = {n: addons.setting(f"COMMUTE_{n.upper()}") for n in FIXED}
    for part in addons.setting("COMMUTE_PLACES").split(";"):
        name, sep, addr = part.partition("=")
        if sep and name.strip() and addr.strip():
            out[name.strip().lower()] = addr.strip()
    return {k: v for k, v in out.items() if v}


def save_place(name: str, addr: str) -> None:
    name = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9 '_-]{0,30}", name):
        raise CommuteError("Keep place names short and plain, like gym or grandma's.")
    if "=" in addr or ";" in addr:
        raise CommuteError("The address can't have = or ; in it.")
    if name in FIXED:
        addons.save({f"COMMUTE_{name.upper()}": addr})
        return
    rest = {k: v for k, v in places().items() if k not in FIXED and k != name}
    rest[name] = addr
    addons.save({"COMMUTE_PLACES": "; ".join(f"{k}={v}" for k, v in rest.items())})


def forget_place(name: str) -> None:
    name = name.strip().lower()
    if name in FIXED:
        addons.save({f"COMMUTE_{name.upper()}": ""})
        return
    rest = {k: v for k, v in places().items() if k not in FIXED}
    if name not in rest:
        raise CommuteError(f"No saved place called {name}.")
    rest.pop(name)
    addons.save({"COMMUTE_PLACES": "; ".join(f"{k}={v}" for k, v in rest.items())})


# -------------------------------------------------------------------- api --

def key() -> str:
    k = addons.setting("TOMTOM_KEY")
    if not k:
        raise CommuteError("Commute isn't set up: no TomTom key. Add a free one in the Atlas app, Add-ons, Commute.")
    return k


def get(path: str, params: dict) -> dict:
    try:
        r = requests.get(TT + path, params={"key": key(), **params}, timeout=20)
    except requests.RequestException as e:
        raise CommuteError(f"Couldn't reach TomTom: {type(e).__name__}.")
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code in (401, 403):
        raise CommuteError("TomTom key rejected. Copy it again from the TomTom dashboard, Keys.")
    if r.status_code == 429:
        raise CommuteError("TomTom's free daily limit is used up. It resets tomorrow.")
    if r.status_code >= 400:
        detail = (j.get("detailedError") or {}).get("message") or (j.get("error") or {}).get("description") or r.text[:150]
        if "no route" in str(detail).lower() or "NO_ROUTE_FOUND" in str(j):
            raise CommuteError("TomTom can't find a driving route between those two.")
        raise CommuteError(f"TomTom said {r.status_code}: {detail}")
    return j


_bias: dict = {}


def city_bias() -> dict:
    """Lat/lon of the owner's city, so "Costco" means the one nearby."""
    if "v" not in _bias:
        _bias["v"] = {}
        if owner.CITY:
            hits = get(f"/search/2/geocode/{urllib.parse.quote(owner.CITY)}.json", {"limit": 1}).get("results", [])
            if hits:
                p = hits[0]["position"]
                _bias["v"] = {"lat": p["lat"], "lon": p["lon"]}
    return _bias["v"]


def locate(ref: str) -> tuple[str, str, float, float]:
    """(label, address for the Maps link, lat, lon)."""
    saved = places()
    name = ref.strip().lower()
    query = saved.get(name, ref.strip())
    label = name.title() if name in saved else ref.strip()
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", query)
    if m:
        return label, f"{m[1]},{m[2]}", float(m[1]), float(m[2])
    params = {"limit": 1, "typeahead": "false", **city_bias()}
    if "lat" in params:
        params["radius"] = 80000
    hits = get(f"/search/2/search/{urllib.parse.quote(query)}.json", params).get("results", [])
    if not hits and "radius" in params:
        params.pop("radius")
        hits = get(f"/search/2/search/{urllib.parse.quote(query)}.json", params).get("results", [])
    if not hits:
        raise CommuteError(f"TomTom couldn't find {query!r}. Try the full address.")
    h = hits[0]
    _country.setdefault("cc", (h.get("address") or {}).get("countryCode", ""))
    addr = (h.get("address") or {}).get("freeformAddress", "")
    poi = (h.get("poi") or {}).get("name", "")
    shown = f"{poi}, {addr}" if poi else addr
    return label, (query if name in saved else shown or query), h["position"]["lat"], h["position"]["lon"]


def maps_link(src: str, dst: str) -> str:
    q = urllib.parse.urlencode({"saddr": src, "daddr": dst, "dirflg": "d"})
    return f"https://maps.apple.com/?{q}"


# ------------------------------------------------------------------ time --

def parse_clock(raw: str, day_raw: str | None) -> datetime:
    s = raw.strip().lower().replace(" ", "").replace(".", "")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", s)
    if not m:
        raise CommuteError(f"Can't read the time {raw!r}. Use 8:30am or 17:30.")
    h, mi, ap = int(m[1]), int(m[2] or 0), m[3]
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        raise CommuteError(f"{raw!r} isn't a real time.")
    now = datetime.now(owner.TZ)
    if day_raw in (None, "", "today"):
        day = now.date()
    elif day_raw == "tomorrow":
        day = now.date() + timedelta(days=1)
    else:
        try:
            day = datetime.strptime(day_raw, "%Y-%m-%d").date()
        except ValueError:
            raise CommuteError("--date is today, tomorrow or YYYY-MM-DD.")
    when = datetime(day.year, day.month, day.day, h, mi, tzinfo=owner.TZ)
    if when < now and day_raw in (None, ""):
        when += timedelta(days=1)  # "--leave 7am" at night means tomorrow morning
    if when < now - timedelta(minutes=1):
        raise CommuteError("That time already passed. TomTom only predicts traffic ahead.")
    return when


def clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def mins(s: int) -> str:
    m = round(s / 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def pop(args: list, flag: str):
    if flag not in args:
        return None
    i = args.index(flag)
    if i + 1 >= len(args):
        raise CommuteError(f"{flag} needs a value.")
    val = args[i + 1]
    del args[i:i + 2]
    return val


def cmd_time(args):
    leave, arrive, day = pop(args, "--leave"), pop(args, "--arrive"), pop(args, "--date")
    if len(args) != 2:
        raise CommuteError('commute.py time FROM TO [--leave 8:30am | --arrive 9am]. Quote addresses with spaces.')
    if leave and arrive:
        raise CommuteError("Pick --leave or --arrive, not both.")
    params = {"traffic": "true", "travelMode": "car", "computeTravelTimeFor": "all", "routeType": "fastest"}
    if leave:
        params["departAt"] = parse_clock(leave, day).isoformat(timespec="seconds")
    elif arrive:
        params["arriveAt"] = parse_clock(arrive, day).isoformat(timespec="seconds")
    a, b = locate(args[0]), locate(args[1])
    route = get(f"/routing/1/calculateRoute/{a[2]},{a[3]}:{b[2]},{b[3]}/json", params)
    s = (route.get("routes") or [{}])[0].get("summary") or {}
    if not s:
        raise CommuteError("TomTom answered without a route.")
    travel, delay = s.get("travelTimeInSeconds", 0), s.get("trafficDelayInSeconds", 0)
    meters = s.get("lengthInMeters", 0)
    dist = f"{meters / 1609.344:.1f} mi" if _country.get("cc") in MILES else f"{meters / 1000:.1f} km"
    dep = datetime.fromisoformat(s["departureTime"]).astimezone(owner.TZ)
    arr = datetime.fromisoformat(s["arrivalTime"]).astimezone(owner.TZ)
    traffic = f", {mins(delay)} of that is traffic" if delay >= 60 else ", traffic is clear"
    when = f"to get there by {clock(arr)}" if arrive else f"leaving {clock(dep)}" if leave else "right now"
    print(f"{a[0]} to {b[0]}: {mins(travel)} {when}{traffic}. {dist}.")
    print(f"Leave {clock(dep)}, arrive {clock(arr)}.")
    print(f"From: {a[1]}")
    print(f"To: {b[1]}")
    print(f"Apple Maps: {maps_link(a[1], b[1])}")


def cmd_link(args):
    if len(args) != 2:
        raise CommuteError("commute.py link FROM TO")
    saved = places()
    src, dst = (saved.get(x.strip().lower(), x.strip()) for x in args)
    print(f"Apple Maps: {maps_link(src, dst)}")


def cmd_places(_args):
    p = places()
    if not p:
        print('No saved places yet. Save one: commute.py save home "address"')
    for k, v in p.items():
        print(f"{k}: {v}")


def cmd_save(args):
    if len(args) < 2:
        raise CommuteError('commute.py save NAME "address"')
    save_place(args[0], " ".join(args[1:]))
    print(f"Saved {args[0].lower()}: {' '.join(args[1:])}")


def cmd_forget(args):
    if not args:
        raise CommuteError("commute.py forget NAME")
    forget_place(args[0])
    print(f"Forgot {args[0].lower()}.")


def cmd_test(_args):
    target = places().get("home") or owner.CITY
    if not target:
        key()
        get("/search/2/geocode/Paris.json", {"limit": 1})
        print("TomTom key works. Add your home address so commutes have a starting point.")
        return
    label, addr, _lat, _lon = locate("home" if "home" in places() else owner.CITY)
    print(f"TomTom key works. {'Home' if 'home' in places() else 'Your city'} found at {addr}.")


COMMANDS = {"time": cmd_time, "link": cmd_link, "places": cmd_places, "save": cmd_save,
            "forget": cmd_forget, "test": cmd_test}


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        return 1
    if argv[0] not in ("test", "link", "places", "save", "forget"):
        addons.require("commute")
    try:
        COMMANDS[argv[0]](argv[1:])
    except CommuteError as e:
        print(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
