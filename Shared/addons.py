r"""Add-ons: the one list of optional things Atlas can do, and their state.

Each add-on has an id, a name, a one-line description, the settings it needs
(with labels, help and where to get a key), whether it's on by default, how
to test it, and a status: off, not_connected, connected or error.

Usage (Atlas runs these):
  python addons.py status            one line per add-on: connected or not, and why
  python addons.py status <id>       just that one
  python addons.py test <id>         run its test now and print the result

Settings live in Shared\.env and go through the same writer the setup
walkthrough uses (App\setup_flow.py), so comments and order survive. Each
add-on's on/off switch is ADDON_<ID>=on|off in the same file. A switch that
was never set means: on for the "On by default" add-ons, and on once its
settings are filled in for the rest (so an install that already had Gmail
set up before add-ons existed doesn't read as off).

Last test results are kept in Shared\addons.json so `status` can say an
add-on is broken without hitting the network. A result only counts while the
add-on's settings are the ones it was tested with.
"""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SHARED = Path(__file__).resolve().parent
ROOT = SHARED.parent
ATLAS = ROOT / "Atlas"
APP = ROOT / "App"
DRY = os.getenv("ATLAS_SETUP_DRYRUN") == "1"  # the app passes this to its children in a dry run
ENV_FILE = SHARED / ("setup-dryrun.env" if DRY else ".env")
STATE_FILE = SHARED / ("addons-dryrun.json" if DRY else "addons.json")
NO_WINDOW = 0x08000000



# ------------------------------------------------------------- settings --

def env() -> dict:
    """Shared\\.env, read fresh every time (tokens get refreshed while the
    bot runs, so a copy read at import would go stale)."""
    from dotenv import dotenv_values
    if not ENV_FILE.exists():
        return {}
    return {k: (v or "").strip() for k, v in dotenv_values(ENV_FILE).items()}


def setting(key: str, default: str = "") -> str:
    """Shared\\.env first, then the process environment (which carries the
    bot folder's .env when a bot runs the script)."""
    return env().get(key) or os.getenv(key, "").strip() or default


# Spotify and Strava send the browser back here after sign-in. Spotify wants
# this exact address in the developer app; Strava only checks the domain.
_port = setting("ADDONS_OAUTH_PORT")
OAUTH_PORT = int(_port) if _port.isdigit() else 47823
REDIRECT_URI = f"http://127.0.0.1:{OAUTH_PORT}/callback"


def save(values: dict) -> None:
    """Write keys into Shared\\.env with the setup walkthrough's own writer."""
    if str(APP) not in sys.path:
        sys.path.append(str(APP))
    import setup_flow
    setup_flow.SetupMixin()._write_env({k: str(v) for k, v in values.items()})


def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(st, indent=1), encoding="utf-8")
    except OSError:
        pass


# ------------------------------------------------------------- registry --

@dataclass
class Field:
    key: str
    label: str
    help: str = ""
    link: str = ""            # where to get it
    link_label: str = ""
    secret: bool = False
    required: bool = True
    placeholder: str = ""
    fallback: str = ""        # another key used when this one is blank (APPLE_ID -> OWNER_EMAIL)


@dataclass
class Addon:
    id: str
    name: str
    desc: str
    needs: str                         # one plain line: what it takes to turn on
    group: str = "connect"             # "default" = works out of the box, "connect" = needs a key or sign-in
    default_on: bool = False
    fields: list = field(default_factory=list)
    tokens: list = field(default_factory=list)   # keys Connect fills in (OAuth tokens and such)
    steps: list = field(default_factory=list)    # how to get the key, in order
    links: list = field(default_factory=list)    # [(label, url)]
    copy: list = field(default_factory=list)     # [(label, text)] values to paste somewhere else
    connect: str = ""                  # "", "oauth", "window" (Apple Music), "job" (runs a command with ASK: prompts)
    connect_label: str = "Connect"
    test: list = field(default_factory=list)     # script and args, from the kit folder
    commands: list = field(default_factory=list) # for the app and Atlas's rules
    icon: str = ""                     # Segoe Fluent Icons code point, for the app

    @property
    def flag(self) -> str:
        return f"ADDON_{self.id.upper()}"


APPLE_ID = Field("APPLE_ID", "Your Apple ID", fallback="OWNER_EMAIL",
                 help="The Apple ID your iPhone uses. Blank means the email from setup.", required=False)

ADDONS: list[Addon] = [
    Addon(
        "weather_alerts", "Weather alerts",
        "A heads-up when rain or snow is about to start, it gets dangerously cold or hot, or there's an official weather warning.",
        "Nothing. Uses the city from setup and the free Open-Meteo forecast.",
        group="default", default_on=True, icon="\uE753",
        fields=[Field("OWNER_CITY", "Your city", help="Alerts follow this city. Add the region if the name is common, like Springfield, Illinois."),
                Field("WEATHER_UNITS", "Units", help="c or f. Blank picks from the country (f in the US).", required=False, placeholder="c"),
                Field("WEATHER_COLD_BELOW", "Cold alert below", help="Feels-like temperature, in the units above. Blank is -25 C or -13 F.", required=False),
                Field("WEATHER_HEAT_ABOVE", "Heat alert above", help="In the units above. Blank is 33 C or 91 F.", required=False)],
        test=["Atlas/weather_alerts.py", "test"],
        commands=["python weather_alerts.py now", "python weather_alerts.py preview"]),
    Addon(
        "voice_notes", "Voice notes",
        "Voice memos you text Atlas get typed out on this PC, so Atlas reads what you said.",
        "Nothing. The speech model (about 140 MB) downloads the first time, or press Download now.",
        group="default", default_on=True, icon="\uE720",
        fields=[Field("VOICE_NOTES_MODEL", "Model", required=False, placeholder="base.en",
                      help="base.en is fast and good for English. small.en is more accurate and slower. base or small for other languages.")],
        connect="job", connect_label="Download now",
        test=["Atlas/voice_notes.py", "status"],
        commands=["python voice_notes.py transcribe <file>"]),
    Addon(
        "calendar", "iCloud calendar",
        "Atlas reads and adds events on your iCloud calendar.",
        "An app-specific password for your Apple ID.",
        icon="\uE787",
        fields=[APPLE_ID,
                Field("APPLE_APP_PASSWORD", "App-specific password", secret=True, placeholder="abcd-efgh-ijkl-mnop",
                      link="https://account.apple.com/account/manage", link_label="Make an app password",
                      help="Sign-In and Security, App-Specific Passwords, then the plus button."),
                Field("CALENDAR_NAME", "Calendar for new events", required=False, help="Blank uses your first calendar.")],
        test=["Atlas/calendar_events.py", "today"],
        commands=["python calendar_events.py today", "python calendar_add.py \"Title\" YYYY-MM-DD [start] [end]"]),
    Addon(
        "reminders", "Reminders",
        "Atlas makes real reminders on your iPhone and reads your Notes.",
        "Sign in to iCloud once on this PC, with the code Apple sends your iPhone.",
        icon="\uE9D5",
        fields=[APPLE_ID,
                Field("REMINDERS_LIST", "List for new reminders", required=False, help="Blank uses Reminders.")],
        connect="job", connect_label="Sign in",
        test=["Atlas/icloud.py", "status"],
        commands=["python icloud.py add \"Title\" [YYYY-MM-DD [time]]", "python icloud.py reminders"]),
    Addon(
        "gmail", "Gmail",
        "Atlas tells you about real emails and watches for ones you're waiting on.",
        "A Google app password (needs 2-step verification on the Google account).",
        icon="\uE715",
        fields=[Field("GMAIL_ADDRESS", "Gmail address", fallback="OWNER_EMAIL", required=False,
                      help="Blank means the email from setup."),
                Field("GMAIL_APP_PASSWORD", "App password", secret=True, placeholder="abcd efgh ijkl mnop",
                      link="https://myaccount.google.com/apppasswords", link_label="Make an app password")],
        test=["Atlas/email_check.py", "24"],
        commands=["python email_check.py 24"]),
    Addon(
        "feeds", "Other calendars",
        "Team schedules, school, work rosters: anything with a calendar link (.ics).",
        "The calendar links, separated by commas.",
        icon="\uE71B",
        fields=[Field("ICS_FEEDS", "Calendar links", placeholder="https://...ics, webcal://...",
                      help="Look for Subscribe or Export calendar in the app that has the schedule.")],
        test=["Atlas/ics_today.py", "week"],
        commands=["python ics_today.py today"]),
    Addon(
        "iris_email", "Iris's email",
        "Gives Iris her own email address for emailing people for you.",
        "A separate Gmail account with an app password.",
        icon="\uE724",
        fields=[Field("IRIS_EMAIL_ADDRESS", "Iris's Gmail"),
                Field("IRIS_EMAIL_APP_PASSWORD", "App password", secret=True,
                      link="https://myaccount.google.com/apppasswords", link_label="Make an app password",
                      help="Make it while signed in to Iris's Gmail, not yours."),
                Field("IRIS_EMAIL_LOGIN", "iCloud login", required=False,
                      help="Only for an @icloud.com alias: the main iCloud Mail address it belongs to.")],
        test=["Iris/email_iris.py", "inbox", "24"],
        commands=["python email_iris.py inbox 24"]),
    Addon(
        "pixabay", "Pictures for Iris",
        "Iris finds real photos to send. Memes work without it.",
        "A free Pixabay key.",
        icon="\uE91B",
        fields=[Field("PIXABAY_KEY", "Pixabay key", secret=True, link="https://pixabay.com/api/docs/", link_label="Get a key",
                      help="Make a free account, then the key is on that page under Parameters.")],
        test=["Shared/addons.py", "_pixabay"],
        commands=["python ../Shared/find_pics.py photos \"words\""]),
    Addon(
        "spotify", "Spotify",
        "What's playing, recent songs, search, play/pause/skip, the queue and playlists.",
        "Your own free Spotify developer app (about 5 minutes). Play, pause and skip need Premium.",
        icon="\uE8D6",
        fields=[Field("SPOTIFY_CLIENT_ID", "Client ID", placeholder="32 letters and numbers",
                      link="https://developer.spotify.com/dashboard", link_label="Open the Spotify dashboard")],
        tokens=["SPOTIFY_REFRESH_TOKEN"],
        steps=["Open the Spotify dashboard and log in with your normal Spotify account. Accept the developer terms if it asks.",
               "Press Create app. Name it Atlas, any description.",
               f"Under Redirect URIs paste {REDIRECT_URI} exactly and press Add.",
               "Tick Web API, agree, and Save.",
               "Open the app's Settings, copy the Client ID, and paste it here. No client secret needed.",
               "Press Connect and approve in the browser."],
        copy=[("Redirect URI", REDIRECT_URI)],
        connect="oauth",
        test=["Atlas/spotify.py", "test"],
        commands=["python spotify.py now", "python spotify.py search \"words\""]),
    Addon(
        "apple_music", "Apple Music",
        "Song and album links work right away. Sign in for recent plays, your library and making playlists.",
        "Nothing for search and links. Your Apple Music sign-in for the library, done in a window here.",
        icon="\uEC4F",
        fields=[Field("APPLE_MUSIC_COUNTRY", "Store country", required=False, placeholder="us",
                      help="Two letters, like us, ca or gb. Blank uses your account's store, or us.")],
        tokens=[],   # search works without any; the library tokens are optional, see status()
        connect="window", connect_label="Sign in",
        test=["Atlas/apple_music.py", "test"],
        commands=["python apple_music.py search \"words\"", "python apple_music.py recent"]),
    Addon(
        "notion", "Notion",
        "Atlas searches, reads and adds to the Notion pages and databases you share with it.",
        "A free internal integration key, and the pages you want it to see shared with it.",
        icon="\uE8A5",
        fields=[Field("NOTION_TOKEN", "Integration secret", secret=True, placeholder="ntn_...",
                      link="https://www.notion.so/profile/integrations", link_label="Open Notion integrations")],
        steps=["Open Notion integrations and press New integration.",
               "Name it Atlas, pick your workspace, type Internal, and save.",
               "Copy the Internal Integration Secret and paste it here.",
               "In Notion, open each page or database Atlas should see, press the ... menu, Connections, and add Atlas. Pages inside a shared page come along."],
        test=["Atlas/notion.py", "test"],
        commands=["python notion.py search \"words\"", "python notion.py read \"page\""]),
    Addon(
        "strava", "Strava",
        "Your recent runs, rides and swims, and weekly totals.",
        "Your own free Strava API app (about 3 minutes).",
        icon="\uE805",
        fields=[Field("STRAVA_CLIENT_ID", "Client ID", placeholder="numbers only",
                      link="https://www.strava.com/settings/api", link_label="Open Strava API settings"),
                Field("STRAVA_CLIENT_SECRET", "Client secret", secret=True)],
        tokens=["STRAVA_REFRESH_TOKEN"],
        steps=["Open Strava API settings, signed in to your Strava account.",
               "Fill in the app form: name Atlas, category Other, website http://127.0.0.1, and Authorization Callback Domain 127.0.0.1",
               "Strava asks for an icon: any small picture works.",
               "Copy the Client ID and Client Secret into the boxes here.",
               "Press Connect and approve in the browser."],
        copy=[("Callback domain", "127.0.0.1")],
        connect="oauth",
        test=["Atlas/strava.py", "test"],
        commands=["python strava.py recent", "python strava.py week"]),
    Addon(
        "commute", "Commute",
        "Drive time with live traffic between your places or any address, with an Apple Maps link to tap.",
        "A free TomTom key (2,500 lookups a day). Saved places are optional.",
        icon="\uE804",
        fields=[Field("TOMTOM_KEY", "TomTom key", secret=True,
                      link="https://developer.tomtom.com/user/register", link_label="Get a free key",
                      help="Sign up, open the Dashboard, Keys, and copy the key it made for you."),
                Field("COMMUTE_HOME", "Home", required=False, placeholder="Street address"),
                Field("COMMUTE_WORK", "Work", required=False, placeholder="Street address"),
                Field("COMMUTE_SCHOOL", "School", required=False, placeholder="Street address"),
                Field("COMMUTE_PLACES", "Other places", required=False, placeholder="gym=123 Main St; rink=Arena name",
                      help="name=address, separated by semicolons. Atlas can add these when you ask it to remember a place.")],
        test=["Atlas/commute.py", "test"],
        commands=["python commute.py time home work", "python commute.py time home \"address\" --arrive 9am"]),
]

BY_ID = {a.id: a for a in ADDONS}


# ---------------------------------------------------------------- status --

def value(f: Field, e: dict | None = None) -> str:
    e = env() if e is None else e
    return e.get(f.key) or (e.get(f.fallback) if f.fallback else "") or ""


def configured(a: Addon, e: dict | None = None) -> list[str]:
    """What's missing: labels of required fields and 'sign-in' for tokens."""
    e = env() if e is None else e
    miss = [f.label for f in a.fields if f.required and not value(f, e)]
    if a.tokens and not all(e.get(k) for k in a.tokens):
        miss.append("sign-in")
    return miss


def switched(a: Addon, e: dict | None = None) -> bool | None:
    """The ADDON_<ID> switch: True, False, or None when it was never set."""
    e = env() if e is None else e
    flag = (e.get(a.flag) or os.getenv(a.flag, "")).strip().lower()
    if flag in ("on", "1", "true", "yes"):
        return True
    if flag in ("off", "0", "false", "no"):
        return False
    return None


def enabled(a: Addon, e: dict | None = None) -> bool:
    e = env() if e is None else e
    s = switched(a, e)
    return s if s is not None else (a.default_on or not configured(a, e))


def signature(a: Addon, e: dict | None = None) -> str:
    """Fingerprint of the settings a test result belongs to."""
    e = env() if e is None else e
    raw = "\n".join(f"{f.key}={value(f, e)}" for f in a.fields) + "".join(
        f"\n{k}={'set' if e.get(k) else ''}" for k in a.tokens)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _extra(a: Addon, e: dict) -> tuple[str, str] | None:
    """Add-on specific status, checked without the network."""
    if a.id == "reminders":
        cookies = Path.home() / ".atlas" / "icloud"
        if not (cookies.is_dir() and any(cookies.iterdir())):
            return "not_connected", "Not signed in yet. Press Sign in."
    if a.id == "apple_music" and not e.get("APPLE_MUSIC_USER_TOKEN"):
        return "connected", "Search and song links work. Sign in for recent plays, your library and playlists."
    if a.id == "voice_notes":
        if importlib.util.find_spec("faster_whisper") is None:
            return "error", "The faster-whisper package isn't installed. Run Setup.cmd again."
    return None


def status(addon_id: str, e: dict | None = None) -> dict:
    a = BY_ID[addon_id]
    e = env() if e is None else e
    out = {"id": a.id, "enabled": enabled(a, e), "state": "connected", "message": "", "tested": "", "tested_ok": None}
    test = _state().get("tests", {}).get(a.id)
    if test and test.get("sig") == signature(a, e):
        out["tested"], out["tested_ok"] = test.get("at", ""), bool(test.get("ok"))
    else:
        test = None
    miss = configured(a, e)
    if switched(a, e) is False or (not out["enabled"] and not miss):
        out["state"], out["message"] = "off", "Turned off."
        return out
    if miss:
        out["state"] = "not_connected"
        out["message"] = ("Not connected yet. Press " + a.connect_label + "." if miss == ["sign-in"]
                          else "Needs: " + ", ".join(m for m in miss if m != "sign-in") + ".")
        return out
    extra = _extra(a, e)
    if extra and extra[0] != "connected":
        out["state"], out["message"] = extra
    elif test and not test.get("ok"):
        out["state"], out["message"] = "error", test.get("message") or "The last test failed."
    elif test:
        out["message"] = test.get("message") or "Works."
    elif extra:
        out["message"] = extra[1]
    else:
        out["message"] = "Set up. Not tested yet."
    return out


def record_test(addon_id: str, ok: bool, message: str) -> None:
    st = _state()
    st.setdefault("tests", {})[addon_id] = {"ok": ok, "message": message[:300],
                                            "at": datetime.now().isoformat(timespec="seconds"),
                                            "sig": signature(BY_ID[addon_id])}
    _save_state(st)


def require(addon_id: str) -> None:
    """For the add-on scripts: stop with a plain line if it's switched off."""
    a = BY_ID[addon_id]
    if not enabled(a):
        sys.exit(f"The {a.name} add-on is turned off. It can be turned on in the Atlas app, Add-ons.")


# ------------------------------------------------------------------ tests --

def python_exe() -> str:
    return sys.executable.replace("pythonw.exe", "python.exe")


def _pixabay() -> int:
    """Pixabay's test: one search, no downloads."""
    import requests
    key = setting("PIXABAY_KEY")
    if not key:
        print("No Pixabay key saved yet.")
        return 1
    try:
        r = requests.get("https://pixabay.com/api/", params={"key": key, "q": "mountain", "per_page": 3}, timeout=20)
    except requests.RequestException as e:
        print(f"Couldn't reach Pixabay: {type(e).__name__}.")
        return 1
    if r.status_code in (400, 401, 403) and "key" in r.text.lower():
        print("Pixabay key rejected. Copy it again from the Pixabay API page.")
        return 1
    if r.status_code >= 400:
        print(f"Pixabay said {r.status_code}: {r.text[:120]}")
        return 1
    print(f"Pixabay works: {r.json().get('totalHits', 0)} photos for a test search.")
    return 0


FAIL_WORDS = ("COULD NOT CHECK", "Traceback (most recent call last)")  # printed by scripts that still exit 0


def run_test(addon_id: str, timeout: int = 90) -> dict:
    """Run an add-on's test. Returns {ok, message, lines} and records it."""
    a = BY_ID[addon_id]
    miss = configured(a)
    if miss:
        res = {"ok": False, "message": "Needs: " + ", ".join(miss) + ".", "lines": []}
        record_test(a.id, False, res["message"])
        return res
    script = ROOT / a.test[0]
    argv = [python_exe(), str(script), *a.test[1:]]
    envp = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
            "HF_HUB_DISABLE_SYMLINKS_WARNING": "1"}
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           cwd=str(script.parent), env=envp, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        code, out = p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        code, out = 124, f"{a.name} didn't answer within {timeout} seconds."
    except OSError as e:
        code, out = 1, f"Couldn't run the test: {e}"
    lines = [l.rstrip() for l in out.splitlines() if l.strip()]
    ok = code == 0 and not any(w in out for w in FAIL_WORDS)
    if ok:
        message = lines[0] if len(lines) == 1 else "Works."
        if a.id in ("calendar", "gmail", "feeds"):
            message = "Works. " + (f"{len(lines)} lines came back." if lines else "Nothing on it right now.")
        elif a.id == "iris_email":
            message = "Works. Iris's inbox opened."
        elif a.id == "reminders":
            message = "Signed in. Reminders work."
    else:
        tail = [l for l in lines if not l.startswith(("  File ", "Traceback"))]
        message = (tail[-1] if tail else f"The test failed (exit {code}).")[:300]
    res = {"ok": ok, "message": message, "lines": lines[-40:]}
    record_test(a.id, ok, message)
    return res


# ------------------------------------------------------------------ OAuth --

def load_script(name: str):
    """Import Atlas\\<name>.py by path (a pip package called spotify must not win)."""
    path = ATLAS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"atlas_addon_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_PAGE = """<!doctype html><meta charset="utf-8"><title>Atlas</title>
<body style="font:16px Segoe UI,sans-serif;background:#202020;color:#fff;display:grid;place-items:center;height:90vh">
<div style="text-align:center"><h2 style="font-weight:600">{title}</h2><p style="color:#bbb">{text}</p></div>"""


class OAuthFlow:
    """One browser sign-in: a tiny server on 127.0.0.1 waits for the
    redirect, then the add-on's script swaps the code for tokens.

    The script provides oauth_start(redirect_uri, state) -> (url, ctx) and
    oauth_finish(params, ctx, redirect_uri) -> message (raises on failure)."""

    current: "OAuthFlow | None" = None

    def __init__(self, addon_id: str, timeout: int = 300):
        self.id = addon_id
        self.state = "waiting"
        self.message = "Waiting for you to approve in the browser."
        self.url = ""
        self._timeout = timeout
        self._server = None
        mod = load_script(addon_id)
        self._mod = mod
        self._csrf = secrets.token_urlsafe(16)
        self.url, self._ctx = mod.oauth_start(REDIRECT_URI, self._csrf)

    def start(self) -> "OAuthFlow":
        if OAuthFlow.current and OAuthFlow.current is not self:
            OAuthFlow.current.cancel("Replaced by a new sign-in.")
        flow = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                if u.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
                ok, title, text = flow._handle(q)
                body = _PAGE.format(title=title, text=text).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                if flow.state != "waiting":
                    threading.Thread(target=flow._stop, daemon=True).start()

        try:
            self._server = http.server.HTTPServer(("127.0.0.1", OAUTH_PORT), Handler)
        except OSError:
            self.state, self.message = "error", (f"Port {OAUTH_PORT} on this PC is busy, so the sign-in can't come back. "
                                                 "Close whatever uses it, or wait a few minutes and try again.")
            return self
        OAuthFlow.current = self
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        threading.Thread(target=self._expire, daemon=True).start()
        return self

    def _handle(self, q: dict) -> tuple[bool, str, str]:
        if self.state != "waiting":
            return False, "Already done", "You can close this tab."
        if q.get("state") != self._csrf:
            return False, "That sign-in didn't match", "Start again from the Atlas app."
        if q.get("error"):
            self.state = "error"
            self.message = "You didn't approve it in the browser." if q["error"] == "access_denied" else f"Sign-in failed: {q['error']}."
            return False, "Not connected", self.message
        try:
            self.message = self._mod.oauth_finish(q, self._ctx, REDIRECT_URI)
            self.state = "done"
            return True, "Connected", "You can close this tab and go back to Atlas."
        except Exception as e:  # noqa: BLE001 - shown to the owner in plain words
            self.state, self.message = "error", str(e) or type(e).__name__
            return False, "Not connected", self.message

    def _expire(self) -> None:
        end = time.time() + self._timeout
        while time.time() < end and self.state == "waiting":
            time.sleep(1)
        if self.state == "waiting":
            self.cancel("The sign-in timed out. Press Connect to try again.")

    def _stop(self) -> None:
        time.sleep(0.5)  # let the page finish sending
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if OAuthFlow.current is self:
            OAuthFlow.current = None

    def cancel(self, why: str = "Cancelled.") -> None:
        if self.state == "waiting":
            self.state, self.message = "error", why
        threading.Thread(target=self._stop, daemon=True).start()


def oauth_login_cli(addon_id: str) -> None:
    """`spotify.py login` and `strava.py login`: the same sign-in from a terminal."""
    import webbrowser
    flow = OAuthFlow(addon_id).start()
    if flow.state == "error":
        sys.exit(flow.message)
    print("Opening the browser to sign in. If it doesn't open, paste this link into it:")
    print(flow.url)
    webbrowser.open(flow.url)
    while flow.state == "waiting":
        time.sleep(0.5)
    if flow.state != "done":
        sys.exit(flow.message)
    print(flow.message)


# -------------------------------------------------------------------- cli --

def _line(a: Addon, s: dict) -> str:
    word = {"connected": "connected", "not_connected": "not connected", "error": "error", "off": "off"}[s["state"]]
    return f"{a.name} ({a.id}): {word}. {s['message']}".rstrip()


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    if cmd == "_pixabay":
        return _pixabay()
    if cmd == "status":
        e = env()
        ids = args or [a.id for a in ADDONS]
        for i in ids:
            if i not in BY_ID:
                print(f"No add-on called {i}. Known: {', '.join(BY_ID)}")
                return 1
            print(_line(BY_ID[i], status(i, e)))
        return 0
    if cmd == "test" and args:
        if args[0] not in BY_ID:
            print(f"No add-on called {args[0]}. Known: {', '.join(BY_ID)}")
            return 1
        r = run_test(args[0])
        print(("Works: " if r["ok"] else "Failed: ") + r["message"])
        return 0 if r["ok"] else 1
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
