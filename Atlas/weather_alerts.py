r"""Weather alerts for the owner's city, for Atlas and its bot loop.

Usage:
  python weather_alerts.py check      new alerts only, one per line; prints NOTHING when there's nothing.
                                      Each alert prints once (the bot runs this hourly).
  python weather_alerts.py preview    every alert that applies right now, without marking any as sent
  python weather_alerts.py now        one line on the weather now, then any alerts
  python weather_alerts.py test       check the city and the forecast

What counts: rain, snow, freezing rain or thunderstorms starting within about
2 hours (when it isn't already coming down), feels-like cold or heat past the
limits in the next 12 hours, and official warnings (Environment Canada in
Canada, the National Weather Service in the US; other countries get the
forecast checks only). Forecasts are Open-Meteo: free, no key.

Settings (Shared\.env): OWNER_CITY (from setup), WEATHER_UNITS (c or f; blank
picks from the country), WEATHER_COLD_BELOW and WEATHER_HEAT_ABOVE (in those
units; blank is -25 C / -13 F and 33 C / 91 F). ADDON_WEATHER_ALERTS=off
silences check.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent / "Shared"))
import addons  # noqa: E402

STATE_FILE = BASE / "weather_alerts.json"
UA = {"User-Agent": "AtlasKit weather alerts"}
SOON_SLOTS = 8          # 15-minute slots ahead = 2 hours
PRECIP_MM = 0.2         # a 15-minute slot this wet counts as "starting"
REPEAT_HOURS = 6        # the same kind of precipitation alert stays quiet this long
# WMO weather codes
RAIN = {51, 53, 55, 61, 63, 65, 80, 81, 82}
FREEZING = {56, 57, 66, 67}
SNOW = {71, 73, 75, 77, 85, 86}
STORM = {95, 96, 99}


class WeatherError(Exception):
    pass


def state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    cut = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    st["sent"] = {k: v for k, v in st.get("sent", {}).items() if v > cut}
    STATE_FILE.write_text(json.dumps(st, indent=1), encoding="utf-8")


def get(url: str, params: dict | None = None) -> dict:
    try:
        r = requests.get(url, params=params, headers=UA, timeout=25)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise WeatherError(f"Couldn't get the weather: {type(e).__name__}.")
    except ValueError:
        raise WeatherError("The weather service sent back something unreadable.")


def place(st: dict) -> dict:
    """The owner's city through Open-Meteo's geocoder, cached by name.
    "Springfield, Illinois" matches the region (or country) after the comma."""
    city = addons.setting("OWNER_CITY")
    if not city:
        raise WeatherError("No city set. Add yours in the Atlas app, Add-ons, Weather alerts.")
    cached = st.get("place") or {}
    if cached.get("query") == city:
        return cached
    name, _, hint = city.partition(",")
    hits = get("https://geocoding-api.open-meteo.com/v1/search",
               {"name": name.strip(), "count": 10, "language": "en", "format": "json"}).get("results") or []
    if not hits:
        raise WeatherError(f"Open-Meteo doesn't know a city called {name.strip()!r}. Check the spelling in Add-ons.")
    hint = hint.strip().lower()
    if hint:
        hits = [h for h in hits if hint in " ".join(str(h.get(k, "")) for k in
                                                    ("admin1", "admin2", "country", "country_code")).lower()] or hits
    h = hits[0]  # results come sorted by population
    st["place"] = {"query": city, "name": h["name"], "region": h.get("admin1", ""), "country": h.get("country_code", ""),
                   "lat": h["latitude"], "lon": h["longitude"], "tz": h.get("timezone", "auto")}
    return st["place"]


def units(p: dict) -> str:
    u = (addons.setting("WEATHER_UNITS") or "").lower()[:1]
    return u if u in ("c", "f") else ("f" if p["country"] in ("US", "LR", "MM") else "c")


def limit(key: str, u: str, c_default: float, f_default: float) -> float:
    try:
        return float(addons.setting(key))
    except ValueError:
        return f_default if u == "f" else c_default


def forecast(p: dict, u: str) -> dict:
    return get("https://api.open-meteo.com/v1/forecast", {
        "latitude": p["lat"], "longitude": p["lon"], "timezone": p["tz"] or "auto",
        "temperature_unit": "fahrenheit" if u == "f" else "celsius",
        "wind_speed_unit": "mph" if u == "f" else "kmh",
        "current": "temperature_2m,apparent_temperature,precipitation,weather_code",
        "minutely_15": "precipitation,rain,snowfall,weather_code",
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,precipitation,weather_code",
        "forecast_minutely_15": SOON_SLOTS + 1, "forecast_hours": 12})


def clock(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%I:%M %p").lstrip("0")


def kind_of(code: int, snow: float, rain: float) -> str:
    if code in STORM:
        return "thunderstorms"
    if code in FREEZING:
        return "freezing rain"
    if code in SNOW or (snow > 0 and snow * 10 >= rain):
        return "snow"
    return "rain"


def precip_alert(f: dict, u: str) -> tuple[str, str] | None:
    cur = f.get("current") or {}
    if (cur.get("precipitation") or 0) >= PRECIP_MM or cur.get("weather_code", 0) in RAIN | FREEZING | SNOW | STORM:
        return None  # already coming down: not news
    m = f.get("minutely_15") or {}
    times = m.get("time") or []
    for i, t in enumerate(times[:SOON_SLOTS + 1]):
        pr = (m.get("precipitation") or [0])[i] or 0
        code = (m.get("weather_code") or [0])[i] or 0
        if pr >= PRECIP_MM or code in STORM | FREEZING:
            kind = kind_of(code, (m.get("snowfall") or [0])[i] or 0, (m.get("rain") or [0])[i] or 0)
            # how much in the first hour after it starts
            total = sum((x or 0) for x in (m.get("precipitation") or [])[i:i + 4])
            size = f"{total / 25.4:.2f} in" if u == "f" else f"{total:.1f} mm"
            amount = f", about {size} in the first hour" if kind != "snow" and total >= 0.1 else ""
            return f"precip:{kind}", f"{kind.capitalize()} starting around {clock(t)}{amount}."
    return None


def temp_alerts(f: dict, u: str) -> list[tuple[str, str]]:
    h = f.get("hourly") or {}
    times, feels, temps = h.get("time") or [], h.get("apparent_temperature") or [], h.get("temperature_2m") or []
    out = []
    cold, heat = limit("WEATHER_COLD_BELOW", u, -25, -13), limit("WEATHER_HEAT_ABOVE", u, 33, 91)
    deg = "F" if u == "f" else "C"
    pairs = [(t, x) for t, x in zip(times, feels) if x is not None]
    if pairs:
        t, low = min(pairs, key=lambda p: p[1])
        if low <= cold:
            out.append((f"cold:{t[:10]}", f"Extreme cold: feels like {round(low)} {deg} around {clock(t)}."))
    pairs = [(t, x) for t, x in zip(times, temps) if x is not None]
    if pairs:
        t, high = max(pairs, key=lambda p: p[1])
        if high >= heat:
            out.append((f"heat:{t[:10]}", f"Extreme heat: {round(high)} {deg} around {clock(t)}."))
    return out


def official_alerts(p: dict) -> list[tuple[str, str]]:
    """Active official warnings for the point. Never raises: a missing
    warnings feed mustn't block the forecast checks."""
    out = []
    try:
        if p["country"] == "US":
            j = get("https://api.weather.gov/alerts/active", {"point": f"{p['lat']:.4f},{p['lon']:.4f}"})
            for feat in j.get("features", []):
                a = feat.get("properties") or {}
                if a.get("severity") in ("Minor", "Unknown") and a.get("event", "").endswith("Statement"):
                    continue
                # keyed by event and start day: an updated alert gets a new id but isn't news
                key = f"official:{a.get('event')}:{(a.get('onset') or a.get('effective') or '')[:10]}"
                out.append((key, f"Official: {a.get('headline') or a.get('event')}"))
        elif p["country"] == "CA":
            d = 0.01
            box = f"{p['lon'] - d},{p['lat'] - d},{p['lon'] + d},{p['lat'] + d}"
            j = get("https://api.weather.gc.ca/collections/weather-alerts/items",
                    {"f": "json", "bbox": box, "limit": 20, "lang": "en"})
            now = datetime.now(timezone.utc).isoformat()
            seen = set()
            for feat in j.get("features", []):
                a = feat.get("properties") or {}
                if (a.get("status_en") or "").lower() == "ended" or (a.get("expiration_datetime") or "9") < now:
                    continue
                name = (a.get("alert_name_en") or "weather alert").strip()
                key = f"official:{a.get('alert_code')}:{a.get('publication_datetime', '')[:13]}"
                if name in seen:
                    continue
                seen.add(name)
                text = (a.get("alert_text_en") or "").strip().split("\n")[0][:220]
                out.append((key, f"Official: {name[0].upper() + name[1:]} for {a.get('feature_name_en') or p['name']}. {text}".strip()))
    except WeatherError:
        pass
    return out


def all_alerts(st: dict) -> tuple[dict, dict, list[tuple[str, str]]]:
    p = place(st)
    u = units(p)
    f = forecast(p, u)
    alerts = []
    pa = precip_alert(f, u)
    if pa:
        alerts.append(pa)
    alerts += temp_alerts(f, u)
    alerts += official_alerts(p)
    return p, f, alerts


def fresh(key: str, sent: dict) -> bool:
    if key not in sent:
        return True
    if key.startswith("precip:"):
        last = datetime.fromisoformat(sent[key])
        return datetime.now(timezone.utc) - last > timedelta(hours=REPEAT_HOURS)
    return False


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cmd = argv[0] if argv else ""
    if cmd not in ("check", "preview", "now", "test"):
        print(__doc__)
        return 1
    if cmd == "check" and not addons.enabled(addons.BY_ID["weather_alerts"]):
        return 0
    st = state()
    try:
        p, f, alerts = all_alerts(st)
    except WeatherError as e:
        if cmd == "check":
            return 0  # the bot loop stays quiet about its own failures
        print(str(e))
        return 1
    where = ", ".join(x for x in (p["name"], p["region"]) if x)
    if cmd == "check":
        sent = st.setdefault("sent", {})
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for k, line in alerts:
            if fresh(k, sent):
                sent[k] = stamp
                print(line)
        save_state(st)
        return 0
    save_state(st)  # keeps the geocode
    if cmd == "preview":
        for _k, line in alerts:
            print(line)
        if not alerts:
            print(f"No weather alerts for {where} right now.")
        return 0
    cur = f.get("current") or {}
    deg = "F" if units(p) == "f" else "C"
    now_line = (f"{where}: {round(cur.get('temperature_2m', 0))} {deg} now, feels like "
                f"{round(cur.get('apparent_temperature', 0))} {deg}.")
    if cmd == "test":
        print(now_line + (f" {len(alerts)} alert(s) right now." if alerts else " No alerts right now."))
        return 0
    print(now_line)
    for _k, line in alerts:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
