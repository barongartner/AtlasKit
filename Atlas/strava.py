r"""Strava for Atlas, through the owner's own Strava API app.

Usage:
  python strava.py recent [n]       the last n activities (default 5): date, type, distance, time, pace
  python strava.py week             this week so far (Monday on), per sport, against last week
  python strava.py weeks [n]        totals for each of the last n weeks (default 4)
  python strava.py test | login     check the connection; sign in from a terminal

Settings (Shared\.env): STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET from
strava.com/settings/api (callback domain 127.0.0.1). Connect in the Atlas app
stores STRAVA_REFRESH_TOKEN; access tokens refresh here (they last 6 hours).
Distances are km or mi from the Strava account's own setting (STRAVA_UNITS).
"""

import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Shared"))
import addons  # noqa: E402
import owner  # noqa: E402

API = "https://www.strava.com/api/v3"
AUTH = "https://www.strava.com/oauth/authorize"
TOKEN = "https://www.strava.com/oauth/token"
RECONNECT = "Strava needs connecting again: Atlas app, Add-ons, Strava, Connect."


class StravaError(Exception):
    pass


# ------------------------------------------------------------------ auth --

def oauth_start(redirect_uri: str, state: str) -> tuple[str, dict]:
    cid = addons.setting("STRAVA_CLIENT_ID")
    if not cid or not addons.setting("STRAVA_CLIENT_SECRET"):
        raise StravaError("Paste the Client ID and Client Secret from your Strava app first.")
    q = {"client_id": cid, "redirect_uri": redirect_uri, "response_type": "code",
         "approval_prompt": "auto", "scope": "read,activity:read_all", "state": state}
    return f"{AUTH}?{urllib.parse.urlencode(q)}", {}


def _token_request(data: dict) -> dict:
    data = {"client_id": addons.setting("STRAVA_CLIENT_ID"),
            "client_secret": addons.setting("STRAVA_CLIENT_SECRET"), **data}
    try:
        r = requests.post(TOKEN, data=data, timeout=20)
    except requests.RequestException as e:
        raise StravaError(f"Couldn't reach Strava: {type(e).__name__}.")
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code >= 400:
        fields = {(e.get("resource"), e.get("field")) for e in j.get("errors", [])}
        if ("Application", "") in fields or any(f[1] in ("client_id", "client_secret") for f in fields):
            raise StravaError("Strava rejected the Client ID or Client Secret. Copy both again from strava.com/settings/api.")
        if any(f[1] in ("code", "refresh_token") for f in fields):
            raise StravaError(RECONNECT)
        raise StravaError(f"Strava said {r.status_code}: {j.get('message') or r.text[:120]}")
    return j


def _store(j: dict) -> None:
    addons.save({"STRAVA_ACCESS_TOKEN": j["access_token"], "STRAVA_REFRESH_TOKEN": j["refresh_token"],
                 "STRAVA_TOKEN_EXPIRES": str(int(j.get("expires_at", time.time() + 3600)) - 60)})


def oauth_finish(params: dict, ctx: dict, redirect_uri: str) -> str:
    if "activity:read" not in params.get("scope", ""):
        raise StravaError("Strava was connected without permission to see activities. "
                          "Press Connect again and leave the activities box ticked.")
    j = _token_request({"code": params.get("code", ""), "grant_type": "authorization_code"})
    _store(j)
    me = call("GET", "/athlete")
    units = "mi" if me.get("measurement_preference") == "feet" else "km"
    addons.save({"STRAVA_UNITS": units, "ADDON_STRAVA": "on"})
    return f"Strava connected as {me.get('firstname', '')} {me.get('lastname', '')}".rstrip() + "."


def access_token(force: bool = False) -> str:
    tok = addons.setting("STRAVA_ACCESS_TOKEN")
    if tok and not force and time.time() < int(addons.setting("STRAVA_TOKEN_EXPIRES") or 0):
        return tok
    refresh = addons.setting("STRAVA_REFRESH_TOKEN")
    if not refresh:
        raise StravaError("Strava isn't connected. Atlas app, Add-ons, Strava, Connect.")
    j = _token_request({"grant_type": "refresh_token", "refresh_token": refresh})
    _store(j)
    return j["access_token"]


def call(method: str, path: str, params=None, retry: bool = True):
    try:
        r = requests.request(method, API + path, params=params, timeout=20,
                             headers={"Authorization": f"Bearer {access_token()}"})
    except requests.RequestException as e:
        raise StravaError(f"Couldn't reach Strava: {type(e).__name__}.")
    if r.status_code == 401 and retry:
        access_token(force=True)
        return call(method, path, params, retry=False)
    if r.status_code == 401:
        raise StravaError(RECONNECT)
    if r.status_code == 429:
        raise StravaError("Strava says slow down (100 requests per 15 minutes). Try again in a bit.")
    if r.status_code >= 400:
        raise StravaError(f"Strava said {r.status_code}: {r.text[:120]}")
    return r.json()


# ---------------------------------------------------------------- format --

def units() -> str:
    return addons.setting("STRAVA_UNITS") or "km"


def dist(m: float) -> str:
    return f"{m / 1609.344:.1f} mi" if units() == "mi" else f"{m / 1000:.1f} km"


def dur(s: int) -> str:
    h, rem = divmod(int(s), 3600)
    return f"{h}h {rem // 60:02d}m" if h else f"{rem // 60}m {rem % 60:02d}s"


def pace(a: dict) -> str:
    m, s, kind = a.get("distance", 0), a.get("moving_time", 0), a.get("sport_type") or a.get("type", "")
    if not m or not s:
        return ""
    if kind in ("Run", "TrailRun", "Walk", "Hike", "VirtualRun"):
        per = s / (m / (1609.344 if units() == "mi" else 1000))
        return f", {int(per // 60)}:{int(per % 60):02d}/{units()}"
    if kind == "Swim":
        per = s / (m / 100)
        return f", {int(per // 60)}:{int(per % 60):02d}/100m"
    speed = (m / s) * (2.23694 if units() == "mi" else 3.6)
    return f", {speed:.1f} {'mph' if units() == 'mi' else 'km/h'}"


def activities(after: datetime, before: datetime | None = None) -> list[dict]:
    out, page = [], 1
    params = {"after": int(after.timestamp()), "per_page": 100}
    if before:
        params["before"] = int(before.timestamp())
    while page <= 5:
        got = call("GET", "/athlete/activities", {**params, "page": page})
        out += got
        if len(got) < 100:
            break
        page += 1
    return out


def totals(acts: list[dict]) -> dict:
    t = defaultdict(lambda: {"n": 0, "m": 0.0, "s": 0, "up": 0.0})
    for a in acts:
        k = a.get("sport_type") or a.get("type", "Other")
        t[k]["n"] += 1
        t[k]["m"] += a.get("distance", 0)
        t[k]["s"] += a.get("moving_time", 0)
        t[k]["up"] += a.get("total_elevation_gain", 0)
    return t


def week_start(now: datetime) -> datetime:
    return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


# -------------------------------------------------------------- commands --

def cmd_recent(args):
    n = int(args[0]) if args and args[0].isdigit() else 5
    acts = call("GET", "/athlete/activities", {"per_page": max(1, min(n, 50))})
    if not acts:
        print("No activities on Strava yet.")
    for a in acts:
        when = datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")).astimezone(owner.TZ)
        print(f"{when.strftime('%a %b %d, %I:%M %p').replace(' 0', ' ')}: {a.get('name', '')} "
              f"({a.get('sport_type') or a.get('type')}) {dist(a.get('distance', 0))} in {dur(a.get('moving_time', 0))}"
              f"{pace(a)} | https://www.strava.com/activities/{a['id']}")


def cmd_week(_args):
    now = datetime.now(owner.TZ)
    this0 = week_start(now)
    last0 = this0 - timedelta(days=7)
    acts = activities(last0)
    this = totals([a for a in acts if datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")) >= this0])
    last = totals([a for a in acts if datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")) < this0])
    print(f"This week (since Monday {this0.strftime('%b %d').replace(' 0', ' ')}):")
    if not this:
        print("Nothing yet.")
    for k, v in sorted(this.items(), key=lambda kv: -kv[1]["s"]):
        lv = last.get(k)
        vs = f" (last week: {dist(lv['m'])}, {lv['n']}x)" if lv else " (none last week)"
        climb = f"{v['up'] * 3.281:.0f} ft" if units() == "mi" else f"{v['up']:.0f} m"
        print(f"{k}: {v['n']}x, {dist(v['m'])}, {dur(v['s'])}, {climb} climbing{vs}")
    for k, v in last.items():
        if k not in this:
            print(f"{k}: none yet (last week: {v['n']}x, {dist(v['m'])})")


def cmd_weeks(args):
    n = int(args[0]) if args and args[0].isdigit() else 4
    now = datetime.now(owner.TZ)
    start = week_start(now) - timedelta(days=7 * (n - 1))
    acts = activities(start)
    for i in range(n):
        w0 = start + timedelta(days=7 * i)
        w1 = w0 + timedelta(days=7)
        t = totals([a for a in acts if w0 <= datetime.fromisoformat(a["start_date"].replace("Z", "+00:00")) < w1])
        parts = [f"{k} {v['n']}x {dist(v['m'])}" for k, v in sorted(t.items(), key=lambda kv: -kv[1]["s"])]
        print(f"Week of {w0.strftime('%b %d').replace(' 0', ' ')}: " + ("; ".join(parts) if parts else "nothing"))


def cmd_test(_args):
    me = call("GET", "/athlete")
    print(f"Strava connected as {me.get('firstname', '')} {me.get('lastname', '')}".rstrip() + ".")


COMMANDS = {"recent": cmd_recent, "week": cmd_week, "weeks": cmd_weeks, "test": cmd_test}


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if argv and argv[0] == "login":
        addons.oauth_login_cli("strava")
        return 0
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        return 1
    if argv[0] != "test":
        addons.require("strava")
    try:
        COMMANDS[argv[0]](argv[1:])
    except StravaError as e:
        print(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
