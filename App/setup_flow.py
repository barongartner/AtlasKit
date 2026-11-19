r"""The setup walkthrough's backend.

The Atlas app mixes SetupMixin into its Api class, so the page can call every
public method here as pywebview.api.setup_*(...). The page (setup.js) owns the
words and the order of the steps; this file owns what actually happens on the
PC: checks, writing Shared\.env, and running long jobs (installs, the bridge
build, the Apple login) with their output streamed back to the page.

Long jobs talk a tiny line protocol (the WSL scripts in Shared\iMessage use it):
  STEP: <what's happening>   OK: <done>   WARN: <...>   ERROR: <what failed>
  RESULT: KEY=VALUE          a value the app saves into Shared\.env

Dry run (`Atlas.pyw --setup --dry-run`, or ATLAS_SETUP_DRYRUN=1) fakes every
job and every change to Windows, WSL and scheduled tasks, so the walkthrough
can be clicked through on a PC that already runs a crew without touching it.
Checks that only read still run for real. Shared\.env is still written in a
dry run (to Shared\setup-dryrun.env instead), so the forms can be tested.
"""

import ctypes
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

APP = Path(__file__).resolve().parent
ROOT = APP.parent
SHARED = ROOT / "Shared"
IM_DIR = SHARED / "iMessage"
EXAMPLE_ENV = SHARED / ".env.example"
DRY = "--dry-run" in sys.argv or os.getenv("ATLAS_SETUP_DRYRUN") == "1"
STATE_FILE = SHARED / ("setup-dryrun.json" if DRY else "setup.json")
ENV_FILE = SHARED / ("setup-dryrun.env" if DRY else ".env")
NO_WINDOW = 0x08000000
NEW_CONSOLE = 0x00000010
SECRET = re.compile(r"TOKEN|PASSWORD|SECRET|KEY$", re.I)
WSL_USER_DEFAULT = "atlas"
BRIDGE_STAGES = ["packages", "toolchains", "source", "build", "download", "synapse", "bridge-config"]
LOGIN_COOLDOWN = 15 * 60   # seconds between Apple login attempts; hammering got an account throttled

sys.path.insert(0, str(SHARED))
import owner  # noqa: E402


# --------------------------------------------------------------- helpers --

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def run(argv, timeout=60, env=None, input_text=None) -> tuple[int, str]:
    """Run a command, return (exit code, stdout+stderr). Never raises."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env, input=input_text, creationflags=NO_WINDOW)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"{argv[0]} isn't installed"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as e:
        return 1, str(e)


def wsl_env() -> dict:
    env = os.environ.copy()
    env["WSL_UTF8"] = "1"  # wsl.exe prints UTF-16 otherwise
    return env


def wsl_path(p: Path) -> str:
    """C:\\Users\\x\\Kit -> /mnt/c/Users/x/Kit"""
    s = str(p.resolve())
    return f"/mnt/{s[0].lower()}{s[2:].replace(chr(92), '/')}" if re.match(r"^[A-Za-z]:\\", s) else s


def claude_exe() -> str:
    found = shutil.which("claude")
    if found:
        return found
    for cand in (Path.home() / ".local" / "bin" / "claude.exe", Path.home() / ".local" / "bin" / "claude"):
        if cand.exists():
            return str(cand)
    return ""


# Emergency numbers people type into the phone box as a joke (North America,
# UK, EU, Australia, Japan and a few others). They get a joke back.
EMERGENCY = {"911", "999", "112", "000", "110", "119", "111", "100", "101", "102", "108"}


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if raw.strip().startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return ""


# Windows time zone id -> IANA, for the common ones (tzutil /g gives the Windows id).
WIN_TZ = {
    "Pacific Standard Time": "America/Los_Angeles", "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix", "Central Standard Time": "America/Chicago",
    "Canada Central Standard Time": "America/Regina", "Eastern Standard Time": "America/New_York",
    "Atlantic Standard Time": "America/Halifax", "Newfoundland Standard Time": "America/St_Johns",
    "Alaskan Standard Time": "America/Anchorage", "Hawaiian Standard Time": "Pacific/Honolulu",
    "GMT Standard Time": "Europe/London", "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris", "Central Europe Standard Time": "Europe/Budapest",
    "AUS Eastern Standard Time": "Australia/Sydney", "New Zealand Standard Time": "Pacific/Auckland",
    "India Standard Time": "Asia/Kolkata", "Tokyo Standard Time": "Asia/Tokyo",
    # A province that went to permanent UTC-6 in Nov 2026; Windows gave it its own zone
    # and tzdata kept the old name with the new rule.
    "Alberta Standard Time": "America/Edmonton",
    "Mountain Standard Time (Mexico)": "America/Mazatlan", "Central America Standard Time": "America/Guatemala",
    "SA Pacific Standard Time": "America/Bogota", "E. South America Standard Time": "America/Sao_Paulo",
    "Greenwich Standard Time": "Atlantic/Reykjavik", "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev", "GTB Standard Time": "Europe/Bucharest", "Russian Standard Time": "Europe/Moscow",
    "South Africa Standard Time": "Africa/Johannesburg", "Arabian Standard Time": "Asia/Dubai",
    "China Standard Time": "Asia/Shanghai", "Singapore Standard Time": "Asia/Singapore",
    "Korea Standard Time": "Asia/Seoul", "W. Australia Standard Time": "Australia/Perth",
    "E. Australia Standard Time": "Australia/Brisbane", "Cen. Australia Standard Time": "Australia/Adelaide",
}

# The walkthrough's picker; an unknown Windows zone falls back to the first of
# these with the same UTC offset right now.
TZ_CHOICES = ["America/St_Johns", "America/Halifax", "America/Toronto", "America/New_York", "America/Winnipeg",
              "America/Chicago", "America/Regina", "America/Edmonton", "America/Denver", "America/Phoenix",
              "America/Vancouver", "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu", "Europe/London",
              "Europe/Dublin", "Europe/Paris", "Europe/Berlin", "Europe/Madrid", "Europe/Rome", "Europe/Amsterdam",
              "Europe/Stockholm", "Europe/Athens", "Asia/Dubai", "Asia/Kolkata", "Asia/Singapore", "Asia/Tokyo",
              "Asia/Seoul", "Australia/Perth", "Australia/Brisbane", "Australia/Sydney", "Pacific/Auckland"]


def windows_tz() -> tuple[str, str]:
    code, out = run(["tzutil", "/g"], timeout=10)
    win = out.strip() if code == 0 else ""
    iana = WIN_TZ.get(win, "")
    if not iana:
        from zoneinfo import ZoneInfo
        here = datetime.now().astimezone().utcoffset()
        iana = next((z for z in TZ_CHOICES if datetime.now(ZoneInfo(z)).utcoffset() == here), "")
    return win, iana


# What the owner can pick in "What do you want Atlas for?". The text is what
# Atlas's CLAUDE.md says about each pick ({OWNER_WANTS} in the template).
USES = {
    "brief": "A daily brief every morning: weather, their day, and the news they care about (Library\\News.md).",
    "track": "Keeping them on track: reminders, deadlines, and following up until things are actually done.",
    "goals": "Goals and habits: regular check-ins on what's in Library\\Goals.md and Library\\Habits.md.",
    "schedule": "Running their schedule: the calendar, plans, and answering \"am I free Thursday\".",
    "email": "Email: a heads-up on the emails that matter, and drafting replies when they ask.",
    "remember": "Remembering things: filing whatever they send you so they can ask for it later.",
    "iris": "Iris texting people for them (that's Iris's job; pass those requests to her on the bus).",
}


def wants_text(env: dict) -> str:
    picks = [p for p in (env.get("OWNER_WANTS") or "").split(",") if p in USES]
    if not picks:
        return ("They haven't said yet what they mainly want from you. Be generally useful, "
                "and notice what they lean on you for.")
    lines = [f"- {USES[p]}" for p in picks]
    unasked = {"brief": "a morning brief", "track": "chasing them about to-dos",
               "goals": "goal or habit check-ins", "email": "email reports"}
    skipped = [unasked[p] for p in unasked if p not in picks]
    tail = (f"\nThey didn't ask for {', '.join(skipped[:-1]) + ' or ' if len(skipped) > 1 else ''}{skipped[-1]}, "
            "so don't start those on your own. If they ask for one later, do it and note it in Memories\\Memory.md.") \
        if skipped else ""
    return "\n".join(lines) + tail


def city_search(q: str) -> list[dict]:
    """Cities matching what's typed, from Open-Meteo's free geocoder (no key).
    Each comes with its time zone and coordinates, so picking a city also
    picks the time zone and gives the weather a real location."""
    import requests
    q = (q or "").strip()
    if len(q) < 2:
        return []
    try:
        r = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": q, "count": 8, "language": "en", "format": "json"},
                         headers={"User-Agent": "atlas-setup/1.0"}, timeout=8)
        r.raise_for_status()
        found = r.json().get("results") or []
    except Exception:
        return [{"error": "Couldn't search cities. Check the internet connection."}]
    out, seen = [], set()
    for c in found:
        parts = [c.get("name"), c.get("admin1"), c.get("country")]
        label = ", ".join(dict.fromkeys(p for p in parts if p))
        if label in seen:
            continue
        seen.add(label)
        out.append({"label": label, "name": c.get("name", ""), "region": c.get("admin1", ""),
                    "country": c.get("country", ""), "lat": c.get("latitude"), "lon": c.get("longitude"),
                    "timezone": c.get("timezone", ""), "population": c.get("population") or 0})
    return out


# ------------------------------------------------------------------ jobs --

class Job:
    """A long command whose output streams to the page. stdin stays open for
    jobs that ask questions (the Apple login)."""

    def __init__(self, label: str, argv=None, env=None, script=None, on_done=None, func=None, stdin_text=None):
        """One of: argv (a process), script (dry-run lines), or func (Python
        run in this thread, writing with job.say and returning an exit code)."""
        self.id = uuid.uuid4().hex[:10]
        self.label = label
        self.text: list[str] = []
        self.results: dict[str, str] = {}
        self.code: int | None = None
        self.proc = None
        self.on_done = on_done
        self._lock = threading.Lock()
        self._partial = ""
        self._script, self._func, self._stdin_text = script, func, stdin_text
        self._argv, self._env = argv, env
        target = self._fake if script is not None else self._call if func is not None else self._real
        threading.Thread(target=target, daemon=True).start()

    def say(self, line: str) -> None:
        self._feed(line.rstrip("\n") + "\n")

    def _call(self) -> None:
        try:
            code = self._func(self)
        except Exception as e:
            self.say(f"ERROR: {e}")
            code = 1
        self._finish(int(code or 0))

    # output
    def _feed(self, chunk: str) -> None:
        with self._lock:
            self.text.append(chunk)
            buf = self._partial + chunk
            lines = buf.split("\n")
            self._partial = lines.pop()
            for line in lines:
                m = re.match(r"\s*RESULT:\s*([A-Z0-9_]+)=(.*)$", line.rstrip("\r"))
                if m:
                    self.results[m[1]] = m[2].strip()

    def _finish(self, code: int) -> None:
        if self._partial:
            self._feed("\n")
        self.code = code
        if self.on_done:
            try:
                self.on_done(self)
            except Exception as e:  # a failed save must still show up on the page
                self._feed(f"ERROR: saving the result failed: {e}\n")

    def _real(self) -> None:
        try:
            self.proc = subprocess.Popen(self._argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, env=self._env, creationflags=NO_WINDOW)
        except OSError as e:
            self._feed(f"ERROR: couldn't start {self._argv[0]}: {e}\n")
            self._finish(127)
            return
        if self._stdin_text is not None:
            try:
                self.proc.stdin.write(self._stdin_text.encode("utf-8"))
                self.proc.stdin.close()
            except OSError:
                pass
        import codecs
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        fd = self.proc.stdout.fileno()
        while True:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            self._feed(dec.decode(data).replace("\r\n", "\n"))
        self._feed(dec.decode(b"", final=True))
        self._finish(self.proc.wait())

    def _fake(self) -> None:
        self._answered = threading.Event()
        for delay, line in self._script:
            time.sleep(delay)
            if self.code is not None:
                return
            self._feed(line + "\n")
            if line.startswith("ASK:"):  # like the real login: wait for the answer
                self._answered.clear()
                while not self._answered.wait(0.5):
                    if self.code is not None:
                        return
        self._finish(0 if not any(l.startswith("ERROR:") for _, l in self._script) else 1)

    # input / control
    def write(self, text: str) -> bool:
        if self._script is not None:
            self._feed("OK: (dry run) got it\n")
            if getattr(self, "_answered", None):
                self._answered.set()
            return True
        if not self.proc or self.proc.poll() is not None:
            return False
        try:
            self.proc.stdin.write((text + "\n").encode("utf-8"))
            self.proc.stdin.flush()
            return True
        except OSError:
            return False

    def cancel(self) -> None:
        if self.proc and self.proc.poll() is None:
            run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)], timeout=15)
        elif self._script is not None and self.code is None:
            self._feed("WARN: cancelled\n")
            self.code = 130

    def poll(self, pos: int) -> dict:
        with self._lock:
            full = "".join(self.text)
        # Hand over whole lines, plus an unfinished last line only when it can't
        # be half of a RESULT (a prompt like "Password: " has no newline yet).
        end = len(full)
        tail = full[full.rfind("\n") + 1:]
        t = tail.lstrip()
        if self.code is None and t and (t.startswith("RESULT:") or "RESULT:".startswith(t)):
            end = len(full) - len(tail)
        chunk = re.sub(r"(RESULT:\s*([A-Z0-9_]+)=)(.*)",
                       lambda m: m[1] + ("(saved)" if SECRET.search(m[2]) else m[3]), full[pos:end])
        return {"text": chunk, "pos": max(pos, end), "done": self.code is not None,
                "code": self.code, "results": {k: ("(saved)" if SECRET.search(k) else v)
                                               for k, v in self.results.items()}}


def bridge_release() -> str:
    """The prebuilt bridge's version when the kit lists one (patches/RELEASE), else ""."""
    f = IM_DIR / "patches" / "RELEASE"
    try:
        vals = dict(l.split("=", 1) for l in f.read_text(encoding="utf-8").splitlines() if "=" in l)
    except OSError:
        return ""
    if not (vals.get("url", "").strip() and vals.get("sha256", "").strip()):
        return ""
    return vals.get("version", "").strip() or "prebuilt"


def fake(*lines: str, step: float = 0.6) -> list[tuple[float, str]]:
    return [(step, l) for l in lines]


# ----------------------------------------------------------------- mixin --

class SetupMixin:
    """Mixed into App/Atlas.pyw's Api. Every public method is page-callable."""

    _jobs: dict = {}

    # ----- state -----

    def _state(self) -> dict:
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"steps": {}, "created": now_iso()}

    def _save_state(self, st: dict) -> None:
        st["updated"] = now_iso()
        STATE_FILE.write_text(json.dumps(st, indent=1), encoding="utf-8")

    def _env_values(self) -> dict:
        from dotenv import dotenv_values
        src = ENV_FILE if ENV_FILE.exists() else EXAMPLE_ENV
        return {k: (v or "").strip() for k, v in dotenv_values(src).items()} if src.exists() else {}

    def _write_env(self, values: dict) -> None:
        """Set keys in Shared\\.env, keeping comments and order."""
        if not ENV_FILE.exists():
            ENV_FILE.write_text(EXAMPLE_ENV.read_text(encoding="utf-8") if EXAMPLE_ENV.exists() else "",
                                encoding="utf-8")
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        seen = set()
        for i, line in enumerate(lines):
            if "=" in line and not line.lstrip().startswith("#"):
                k = line.partition("=")[0].strip()
                if k in values:
                    lines[i] = f"{k}={values[k]}"
                    seen.add(k)
        lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # owner.py read the file at import: load it again so checks see the new values.
        os.environ.update({k: str(v) for k, v in values.items()})
        importlib.reload(owner)

    def _wsl_user(self) -> str:
        return self._env_values().get("WSL_USER") or WSL_USER_DEFAULT

    def _distro(self) -> str:
        return self._env_values().get("WSL_DISTRO") or owner.WSL_DISTRO

    def setup_state(self):
        st = self._state()
        env = self._env_values()
        shown = {k: ("" if SECRET.search(k) else v) for k, v in env.items()}
        have = {k: bool(v) for k, v in env.items() if SECRET.search(k)}
        win, iana = windows_tz()
        last = st.get("last_login_try", 0)
        return {"dry": DRY, "steps": st.get("steps", {}), "env": shown, "secretsSet": have,
                "windowsTz": win, "windowsTzIana": iana, "kit": str(ROOT),
                "loginWait": max(0, int(LOGIN_COOLDOWN - (time.time() - last))),
                "macCommand": self._mac_command(), "distro": self._distro(), "tailor": st.get("tailor", {}),
                "bridgeRelease": bridge_release()}

    def setup_mark(self, step, status):
        st = self._state()
        st.setdefault("steps", {})[str(step)] = {"status": status, "at": now_iso()}
        self._save_state(st)
        return True

    def setup_done(self) -> bool:
        return self._state().get("steps", {}).get("finish", {}).get("status") == "done"

    # ----- forms -----

    def setup_save(self, values):
        """Validate and save a form. Returns {ok, errors: {field: message}}."""
        if not isinstance(values, dict):
            return {"ok": False, "errors": {"_": "Nothing to save."}}
        errors, clean = {}, {}
        email_mode = bool(str(values.get("OWNER_IMESSAGE") or "").strip())
        for k, v in values.items():
            v = str(v or "").strip()
            if k == "OWNER_PHONE" and not v and email_mode:
                pass  # they text from their Apple ID email instead of a number
            elif k == "OWNER_IMESSAGE":
                handles = [h.strip() for h in v.split(",") if h.strip()]
                bad = [h for h in handles if "@" in h and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", h)]
                if bad:
                    errors[k] = "That doesn't look like an email address."
                v = ",".join(h.lower() if "@" in h else (normalize_phone(h) or h) for h in handles)
            elif k == "OWNER_PHONE":
                p = normalize_phone(v)
                if re.sub(r"\D", "", v) in EMERGENCY:
                    errors[k] = ("Atlas texting the cops your morning brief every day is one way to meet them. "
                                 "Put your own number.")
                elif not p:
                    errors[k] = "Use the full number, like 403 555 0123 or +44 7700 900123."
                v = p
            elif k == "OWNER_NAME" and not v:
                errors[k] = "Put your first name."
            elif k in ("OWNER_EMAIL", "BOT_APPLE_ID", "GMAIL_ADDRESS", "IRIS_EMAIL_ADDRESS", "APPLE_ID") and v \
                    and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
                errors[k] = "That doesn't look like an email address."
            elif k == "BOT_APPLE_ID" and "+" in v.partition("@")[0]:
                errors[k] = "Use the plain address, without a +something. The app adds +atlas and +iris itself."
            elif k == "OWNER_TIMEZONE":
                try:
                    from zoneinfo import ZoneInfo
                    ZoneInfo(v)
                except Exception:
                    errors[k] = "Pick a time zone from the list."
            elif k == "CLAUDE_CODE_OAUTH_TOKEN" and v and not v.startswith("sk-ant-"):
                errors[k] = "That isn't a Claude token. It starts with sk-ant-."
            elif k.endswith("APP_PASSWORD"):
                v = v.replace(" ", "")
            clean[k] = v
        if errors:
            return {"ok": False, "errors": errors}
        # OWNER_EMAIL (Gmail checks, calendar) is never trusted as an owner
        # handle by itself: only what's in OWNER_PHONE or OWNER_IMESSAGE is.
        self._write_env(clean)
        if clean.get("OWNER_NAME") or "OWNER_WANTS" in clean:
            self._fill_placeholders()
        return {"ok": True, "errors": {}}

    def setup_city_search(self, q):
        return city_search(q)

    # ----- "Make it yours": the brief, news, goals and habits -----

    def setup_tailor(self, data):
        """Write the follow-up answers into Atlas's Library. `data`:
        brief_time ("07:30", "random 09:00-10:00" or "off"), beats (list of
        {label, text} in order), goals (list of str), habits (list of
        {name, gist}), teams (list of {league, team}). Saved in setup.json too,
        so the step shows the answers again. In a dry run the files go to
        Shared\\setup-dryrun-preview\\Atlas\\Library instead."""
        if not isinstance(data, dict):
            return {"ok": False, "message": "Nothing to save."}
        lib = ROOT / "Atlas" / "Library"
        out_dir = SHARED / "setup-dryrun-preview" / "Atlas" / "Library" if DRY else lib
        out_dir.mkdir(parents=True, exist_ok=True)
        name = self._env_values().get("OWNER_NAME") or owner.NAME
        today = datetime.now().strftime("%Y-%m-%d")

        def read(fname):
            for p in (out_dir / fname, lib / fname):
                if p.exists():
                    return p.read_text(encoding="utf-8").replace("{OWNER_NAME}", name)
            return ""

        def write(fname, text):
            (out_dir / fname).write_text(text.rstrip() + "\n", encoding="utf-8")

        def clean(s, n=300):
            return re.sub(r"\s+", " ", str(s or "")).strip()[:n]

        # Settings.md: the brief time
        bt = clean(data.get("brief_time")) or "random 09:00-10:00"
        if not re.fullmatch(r"off|random \d{1,2}:\d{2}-\d{1,2}:\d{2}|\d{1,2}:\d{2}", bt):
            bt = "random 09:00-10:00"
        s = read("Settings.md")
        if s:
            s = re.sub(r"(?m)^- Brief time:.*$", f"- Brief time: {bt}", s)
            write("Settings.md", s)

        # News.md: the beats, in their order
        beats = [b for b in (data.get("beats") or []) if isinstance(b, dict) and clean(b.get("label"))]
        n = read("News.md")
        if n and beats:
            lines = [f"{i}. **{clean(b['label'], 60)}** - {clean(b.get('text'))}" for i, b in enumerate(beats, 1)]
            section = ("## The beats, in this order\n\nOne item per beat, two at most on a big day. Skip a beat if nothing "
                       "real happened in the last 24 to 48 hours, but never skip the whole section.\n\n" + "\n".join(lines) +
                       f"\n\n{name} picked these in setup. Add, drop or reorder beats when they ask for more or less of something, "
                       "and note new interests you learn about here.\n")
            n = re.sub(r"(?s)## The beats, in this order\n.*?(?=\n## |\Z)", section.rstrip("\n") + "\n", n, count=1)
            write("News.md", n)

        # Goals.md and Habits.md: add what's new under Active
        def add_under_active(fname, entries):
            text = read(fname)
            if not text or not entries:
                return
            fresh = [e for e in entries if e.split("\n")[0] not in text]
            if fresh:
                text = text.replace("## Active\n", "## Active\n\n" + "\n".join(fresh) + "\n", 1)
                write(fname, re.sub(r"\n{3,}", "\n\n", text))

        goals = [clean(g) for g in (data.get("goals") or []) if clean(g)]
        add_under_active("Goals.md", [f"- {g} (since {today})" for g in goals])
        habits = [h for h in (data.get("habits") or []) if isinstance(h, dict) and clean(h.get("name"))]
        cap = lambda s: s[:1].upper() + s[1:]
        add_under_active("Habits.md", [f"### {cap(clean(h['name'], 60))}\n- Started: {today}\n- The gist: {clean(h.get('gist')) or clean(h['name'])}\n- Log:"
                                       for h in habits])

        # Sports teams become watches (final scores), when the watches system is in.
        teams = [t for t in (data.get("teams") or []) if isinstance(t, dict) and clean(t.get("team"))]
        warn = ""
        add_teams = getattr(self, "watches_add_teams", None)
        if teams and add_teams and not DRY:
            try:
                res = add_teams([{"league": t.get("league"), "team": t.get("team")} for t in teams])
                bad = [f"{e['team']} ({e['league']})" for e in (res or {}).get("errors", [])]
                if bad:
                    warn = (f"Couldn't find {', '.join(bad)} for score alerts. They're still in the brief; "
                            "fix the name on the Watches page.")
            except Exception as e:
                warn = f"The score alerts didn't save ({e}). Add them on the Watches page."

        st = self._state()
        st["tailor"] = data
        self._save_state(st)
        where = "Shared\\setup-dryrun-preview" if DRY else "Atlas's Library"
        return {"ok": True, "message": f"Saved to {where}.", "warn": warn}

    # ----- the owner's name (and what they want Atlas for) in the bots' files -----

    def _fill_placeholders(self) -> list[str]:
        """Put the owner's name (and their picks) into the bots' files.

        CLAUDE.md files keep a CLAUDE.template.md copy with the placeholders,
        so a changed name or new picks are filled in again from scratch. The
        starter Library and Memories files are the bots' own to edit, so they
        get filled once in place; a later name change swaps the old name for
        the new one in them. In a dry run nothing real changes: filled copies
        go to Shared\\setup-dryrun-preview\\ to look at instead."""
        env = self._env_values()
        name = env.get("OWNER_NAME", "").strip()
        if not name:
            return []
        fills = {"{OWNER_NAME}": name, "{OWNER_WANTS}": wants_text(env)}
        st = self._state()
        old = st.get("filled_name", "")
        preview = SHARED / "setup-dryrun-preview"
        done = []
        for bot in ("Atlas", "Iris"):
            base = ROOT / bot
            md, tpl = base / "CLAUDE.md", base / "CLAUDE.template.md"
            files = [(md, tpl)] + [(f, None) for sub in ("Library", "Memories") for f in sorted((base / sub).glob("*.md"))]
            for target, template in files:
                if template is not None:
                    if not template.exists() and target.exists():
                        if DRY:
                            template = target  # read the placeholder copy straight from CLAUDE.md
                        else:
                            shutil.copyfile(target, template)
                    if not template.exists():
                        continue
                    text = template.read_text(encoding="utf-8")
                else:
                    if not target.exists() or target.name == "Archive.md":
                        continue
                    text = target.read_text(encoding="utf-8")
                    if old and old != name and "{OWNER_NAME}" not in text:
                        text = re.sub(rf"\b{re.escape(old)}\b", name, text)
                new = text
                for k, v in fills.items():
                    new = new.replace(k, v)
                if template is None and new == target.read_text(encoding="utf-8"):
                    continue
                out = preview / bot / target.relative_to(base) if DRY else target
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(new, encoding="utf-8")
                done.append(str(target.relative_to(ROOT)))
        st = self._state()
        st["filled_name"] = name
        self._save_state(st)
        return done

    # ----- checks (read-only, run for real even in a dry run) -----

    def setup_check(self, step):
        fn = getattr(self, f"_check_{step}", None)
        return fn() if fn else []

    def _check_pc(self):
        items = []
        build = sys.getwindowsversion().build
        items.append({"id": "windows", "label": "Windows 10 (2004 or newer) or Windows 11",
                      "ok": build >= 19041, "detail": f"Build {build}"})
        code, out = run(["powershell", "-NoProfile", "-Command",
                         "(Get-CimInstance Win32_Processor | Select-Object -First 1).VirtualizationFirmwareEnabled;"
                         "(Get-CimInstance Win32_ComputerSystem).HypervisorPresent"], timeout=30)
        vals = [l.strip().lower() for l in out.splitlines() if l.strip()]
        virt = "true" in vals
        items.append({"id": "virt", "label": "Virtualization is turned on", "ok": virt,
                      "detail": "On" if virt else "Turn on Intel VT-x or AMD SVM in the BIOS, then come back."})
        free = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\").free / 2**30
        need = 8 if bridge_release() else 25  # a downloaded bridge skips the compilers and the build
        items.append({"id": "disk", "label": f"{need} GB free on the system drive", "ok": free >= need,
                      "detail": f"{free:.0f} GB free"})
        items.append({"id": "python", "label": "Python 3.11 or newer", "ok": sys.version_info >= (3, 11),
                      "detail": sys.version.split()[0]})
        code, out = run(["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"], timeout=15)
        m = re.findall(r"Current AC Power Setting Index:\s*0x([0-9a-f]+)", out, re.I)
        never = bool(m) and int(m[0], 16) == 0
        items.append({"id": "sleep", "label": "The PC never sleeps on its own while plugged in", "ok": never,
                      "detail": "Never" if never else (f"Sleeps after {int(m[0], 16) // 60} minutes" if m else "Couldn't read it"),
                      "fix": None if never else "sleep_never"})
        return items

    def _check_claude(self):
        exe = claude_exe()
        ver = ""
        if exe:
            code, out = run([exe, "--version"], timeout=30)
            ver = out.strip().splitlines()[0] if code == 0 and out.strip() else ""
        token = bool(self._env_values().get("CLAUDE_CODE_OAUTH_TOKEN"))
        return [{"id": "cli", "label": "Claude Code is installed", "ok": bool(ver), "detail": ver or "Not found"},
                {"id": "token", "label": "Claude login token saved", "ok": token,
                 "detail": "Saved" if token else "Not yet"}]

    def _check_wsl(self):
        distro = self._distro()
        code, out = run(["wsl", "--status"], timeout=30, env=wsl_env())
        wsl_ok = code == 0
        code2, out2 = run(["wsl", "-l", "-q"], timeout=30, env=wsl_env())
        names = [l.strip().strip("\x00") for l in out2.splitlines() if l.strip()]
        have = distro in names
        user_ok = False
        if have and not DRY:
            c3, o3 = run(["wsl", "-d", distro, "-u", "root", "-e", "id", "-u", self._wsl_user()], timeout=60, env=wsl_env())
            user_ok = c3 == 0
        elif have:
            user_ok = True
        task = run(["schtasks", "/query", "/tn", "Atlas WSL Keepalive"], timeout=15)[0] == 0
        return [{"id": "wsl", "label": "WSL is installed and working", "ok": wsl_ok,
                 "detail": "Working" if wsl_ok else "Not installed yet, or Windows needs a restart"},
                {"id": "distro", "label": f"{distro} is installed", "ok": have, "detail": "Installed" if have else "Not yet"},
                {"id": "user", "label": f"Linux user '{self._wsl_user()}' exists", "ok": user_ok,
                 "detail": "Ready" if user_ok else "Not yet"},
                {"id": "keepalive", "label": "Keepalive task (keeps WSL running)", "ok": task,
                 "detail": "Set up" if task else "Not yet"}]

    def _check_bridge(self):
        st = self._state().get("steps", {})
        return [{"id": s, "label": s, "ok": st.get(f"bridge:{s}", {}).get("status") == "done",
                 "detail": st.get(f"bridge:{s}", {}).get("at", "")} for s in BRIDGE_STAGES]

    # ----- fixes -----

    def setup_fix(self, fix_id):
        if fix_id == "sleep_never":
            if DRY:
                return {"ok": True, "message": "(dry run) would set sleep to Never while plugged in"}
            code, out = run(["powercfg", "/change", "standby-timeout-ac", "0"], timeout=20)
            return {"ok": code == 0, "message": "Sleep is set to Never while plugged in." if code == 0 else out.strip()}
        return {"ok": False, "message": "Unknown fix"}

    # ----- one-shot actions -----

    def setup_open(self, target):
        """Open a web page or a kit folder in the normal Windows app."""
        allowed = target.startswith(("https://", "http://")) or target in ("kit",)
        if not allowed:
            return False
        os.startfile(str(ROOT) if target == "kit" else target)
        return True

    def setup_copy(self, text):
        """Clipboard fallback for when the page's own copy is blocked."""
        try:
            subprocess.run(["clip"], input=str(text).encode("utf-16-le"), creationflags=NO_WINDOW, timeout=10)
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False

    def setup_claude_signin(self):
        """`claude setup-token` needs a browser sign-in, so it runs in its own
        console window; they paste the token it prints back into the app."""
        exe = claude_exe()
        if not exe:
            return {"ok": False, "message": "Install Claude Code first."}
        if DRY:
            return {"ok": True, "message": "(dry run) would open a window running claude setup-token"}
        subprocess.Popen(["cmd", "/k", exe, "setup-token"], creationflags=NEW_CONSOLE)
        return {"ok": True, "message": "A window opened. Sign in there, then copy the token it shows."}

    def setup_wsl_install(self):
        """WSL needs admin rights: this shows the Windows permission prompt."""
        distro = self._distro()
        if DRY:
            return {"ok": True, "message": f"(dry run) would ask for admin and run wsl --install -d {distro}"}
        r = ctypes.windll.shell32.ShellExecuteW(None, "runas", "wsl.exe",
                                                f"--install -d {distro} --no-launch", None, 1)
        if r <= 32:
            return {"ok": False, "message": "Windows didn't start the install (the permission prompt was declined?)."}
        return {"ok": True, "message": "The installer opened in its own window. When it finishes, press Check again. "
                                       "If it says to restart, restart the PC and open Atlas again."}

    def _mac_command(self) -> str:
        f = IM_DIR / "scripts" / "mac-info.oneline.txt"
        try:
            return f.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    # ----- long jobs -----

    def _wsl(self, *cmd, root=False, extra_env=None):
        distro = self._distro()
        envs = {"BRIDGE_DIR": f"/home/{self._wsl_user()}/imessage-bridge" if not root else "",
                "MATRIX_SERVER_NAME": owner.MATRIX_SERVER, "KIT_IMESSAGE": wsl_path(IM_DIR),
                "BOT_USER": "atlas", "BOT_APPLE_ID": owner.BOT_APPLE_ID, **(extra_env or {})}
        pairs = [f"{k}={v}" for k, v in envs.items() if v]
        return ["wsl", "-d", distro, "-u", "root" if root else self._wsl_user(), "-e", "env", *pairs, *cmd]

    def _start(self, label, argv=None, script=None, on_done=None, env=None, func=None, stdin_text=None):
        job = Job(label, argv=argv, env=env or wsl_env(), script=script, on_done=on_done,
                  func=func, stdin_text=stdin_text)
        self._jobs[job.id] = job
        return {"job": job.id}

    def _save_results(self, job: Job):
        if job.code == 0 and job.results:
            self._write_env(job.results)

    def setup_run(self, action, params=None):
        params = params or {}
        im = wsl_path(IM_DIR)
        if action == "claude_install":
            if DRY:
                return self._start("Install Claude Code", script=fake(
                    "STEP: Downloading the Claude Code installer", "STEP: Installing", "OK: Claude Code is installed"))
            return self._start("Install Claude Code", argv=["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                                             "-Command", "irm https://claude.ai/install.ps1 | iex"],
                               env=os.environ.copy())
        if action == "claude_test":
            if DRY:
                return self._start("Test the Claude login", script=fake("STEP: Asking Claude to reply OK", "OK"))
            env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_", "CLAUDECODE"))}
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self._env_values().get("CLAUDE_CODE_OAUTH_TOKEN", "")
            exe = claude_exe() or "claude"
            return self._start("Test the Claude login", argv=[exe, "-p", "--model", "haiku"], env=env,
                               stdin_text="Reply with exactly: OK")
        if action == "wsl_user":
            user = self._wsl_user()
            script = (f"id -u {user} >/dev/null 2>&1 || useradd -m -s /bin/bash {user}; passwd -d {user} >/dev/null; "
                      f"echo '{user} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/{user}; chmod 440 /etc/sudoers.d/{user}; "
                      f"grep -q '^default={user}' /etc/wsl.conf 2>/dev/null || printf '[user]\\ndefault={user}\\n' >> /etc/wsl.conf; "
                      f"echo 'OK: Linux user {user} is ready'")
            if DRY:
                return self._start("Create the Linux user", script=fake(f"STEP: Creating user {user}", f"OK: Linux user {user} is ready"))
            return self._start("Create the Linux user", argv=["wsl", "-d", self._distro(), "-u", "root", "-e", "bash", "-c", script])
        if action == "keepalive":
            return self._task_job("Atlas WSL Keepalive", f"--headless wsl.exe -d {self._distro()} -- sleep infinity",
                                  every_minutes=0, run_now=True)
        if action == "bridge_stage":
            stage = params.get("stage")
            if stage not in BRIDGE_STAGES:
                return {"error": "unknown stage"}
            def done(job, stage=stage):
                if job.code == 0:
                    self.setup_mark(f"bridge:{stage}", "done")
            if DRY:
                return self._start(f"Bridge: {stage}", on_done=done, script=fake(
                    f"STEP: (dry run) {stage}: getting ready", f"STEP: (dry run) {stage}: working",
                    f"OK: (dry run) {stage} finished", step=0.8))
            return self._start(f"Bridge: {stage}", on_done=done,
                               argv=self._wsl("bash", f"{im}/scripts/install.sh", stage, root=(stage == "packages")))
        if action == "mac_key":
            raw = str(params.get("json") or "").strip()
            try:
                data = json.loads(raw)
            except ValueError:
                return {"error": "That isn't the line the Mac printed. Copy the whole line, from { to }."}
            if data.get("error") == "apple_silicon" or "apple" in str(data.get("cpu", "")).lower():
                return {"error": "This Mac is Apple Silicon. The bridge needs an Intel Mac's identity."}
            if DRY:
                return self._start("Save the Mac identity", script=fake("STEP: Checking the values", "OK: (dry run) Mac identity saved"))
            tmp = Path(tempfile.gettempdir()) / f"atlas-mac-{uuid.uuid4().hex[:8]}.json"
            tmp.write_text(json.dumps(data), encoding="utf-8")

            def cleanup(job, tmp=tmp):
                tmp.unlink(missing_ok=True)
            return self._start("Save the Mac identity", on_done=cleanup,
                               argv=self._wsl("python3", f"{im}/scripts/make-key.py", wsl_path(tmp)))
        if action == "apple_login":
            st = self._state()
            wait = LOGIN_COOLDOWN - (time.time() - st.get("last_login_try", 0))
            if wait > 0:
                return {"error": f"Wait {int(wait // 60) + 1} more minutes before trying again. Fast retries are how Apple locks the account."}
            st["last_login_try"] = time.time()
            self._save_state(st)
            def done(job):
                if job.code == 0:
                    self.setup_mark("apple_login", "done")
            if DRY:
                return self._start("Apple login", on_done=done, script=[
                    (0.6, "STEP: (dry run) Starting the bridge for the login"), (0.8, "ASK: apple_id Enter the bots' Apple ID (its email address)."),
                    (0.4, "ASK: password Enter the password of the bots' Apple ID."), (1.2, "STEP: (dry run) Checking the password with Apple"),
                    (0.8, "ASK: code Enter the 6-digit code Apple just sent (a notification on a trusted device, or a text or voice call to the trusted phone number)."),
                    (1.0, "STEP: (dry run) Registering with iMessage"), (1.2, "OK: (dry run) Logged in")])
            return self._start("Apple login", on_done=done, argv=self._wsl("bash", f"{im}/scripts/login.sh"))
        if action == "bridge_start":
            if DRY:
                return self._start("Start the bridge", script=fake("synapse up", "bridge up"))
            return self._start("Start the bridge", argv=self._wsl("bash", f"/home/{self._wsl_user()}/imessage-bridge/start.sh"))
        if action == "matrix_setup":
            if DRY:
                return self._start("Connect the bots", on_done=self._save_results, script=fake(
                    "STEP: (dry run) Making the bots' Matrix user", "STEP: (dry run) Finding the bridge's room",
                    "RESULT: IMESSAGE_MGMT_ROOM=!dryrun:atlas.local", "OK: (dry run) connected"))
            return self._start("Connect the bots", on_done=self._save_results, argv=self._wsl("bash", f"{im}/scripts/matrix-setup.sh"))
        if action == "bridge_task":
            u = self._wsl_user()
            return self._task_job("Atlas iMessage Bridge",
                                  f"--headless wsl.exe -d {self._distro()} -u {u} -- bash -lc ~/imessage-bridge/start.sh",
                                  every_minutes=2, run_now=True)
        if action == "crew_task":
            return self._task_job("Atlas Crew", f'--headless cmd.exe /c "{SHARED / "Start Crew.cmd"}"',
                                  every_minutes=0, run_now=False)
        if action == "test":
            return self._extra_test(params.get("what", ""))
        if action == "finish":
            return self._finish_job()
        return {"error": f"unknown action {action}"}

    def _task_job(self, name, args, every_minutes=0, run_now=False):
        """A scheduled task that starts at logon (and repeats every N minutes),
        windowless through conhost --headless, the same way the original crew
        does it."""
        trig = ""
        if every_minutes:
            trig = (f"<TimeTrigger><Repetition><Interval>PT{every_minutes}M</Interval></Repetition>"
                    f"<StartBoundary>2026-01-01T00:00:00</StartBoundary><Enabled>true</Enabled></TimeTrigger>")
        limit = "PT5M" if every_minutes else "PT0S"
        xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{os.environ.get("USERDOMAIN", "")}\\{os.environ.get("USERNAME", "")}</UserId></LogonTrigger>{trig}</Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>{limit}</ExecutionTimeLimit><Enabled>true</Enabled></Settings>
  <Actions Context="Author"><Exec><Command>conhost.exe</Command><Arguments>{args.replace("&", "&amp;").replace('"', "&quot;")}</Arguments></Exec></Actions>
</Task>"""
        if DRY:
            return self._start(f"Task: {name}", script=fake(f"STEP: (dry run) would create the task {name}",
                                                            f"STEP: (dry run) conhost.exe {args}", f"OK: (dry run) {name} set up"))
        def make(job):
            tmp = Path(tempfile.gettempdir()) / f"atlas-task-{uuid.uuid4().hex[:8]}.xml"
            try:
                tmp.write_text(xml, encoding="utf-16")
                job.say(f"STEP: Creating the scheduled task {name}")
                code, out = run(["schtasks", "/create", "/f", "/tn", name, "/xml", str(tmp)], timeout=30)
                if code:
                    job.say(f"ERROR: Windows wouldn't create the task: {out.strip()}")
                    return code
                if run_now:
                    job.say(f"STEP: Starting it")
                    code, out = run(["schtasks", "/run", "/tn", name], timeout=30)
                    if code:
                        job.say(f"ERROR: The task was made but didn't start: {out.strip()}")
                        return code
                job.say(f"OK: {name} is set up")
                return 0
            finally:
                tmp.unlink(missing_ok=True)
        return self._start(f"Task: {name}", func=make)

    def _extra_test(self, what):
        py = sys.executable.replace("pythonw.exe", "python.exe")
        tests = {
            "calendar": ([py, str(ROOT / "Atlas" / "calendar_events.py"), "today"], "Reading today's calendar"),
            "gmail": ([py, str(ROOT / "Atlas" / "email_check.py"), "24"], "Checking the inbox"),
            "feeds": ([py, str(ROOT / "Atlas" / "ics_today.py"), "week"], "Reading the extra calendars"),
            "iris_email": ([py, str(ROOT / "Iris" / "email_iris.py"), "inbox", "24"], "Checking Iris's inbox"),
            "icloud": ([py, str(ROOT / "Atlas" / "icloud.py"), "login"], "Reminders login"),
        }
        if what not in tests:
            return {"error": "unknown test"}
        argv, label = tests[what]
        if DRY:
            return self._start(label, script=fake(f"STEP: (dry run) {label}", "OK: (dry run) works"))
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        return self._start(label, argv=argv, env=env)

    def _finish_job(self):
        """Fill the owner's name into the bots' rules, start the crew, make the
        logon task, and text the owner."""
        name = owner.NAME
        if DRY:
            return self._start("Finish", script=fake(
                f"OK: (dry run) the filled-in files are in Shared\\setup-dryrun-preview, using the name {name}",
                "STEP: (dry run) starting Atlas and Iris", "STEP: (dry run) texting you from Atlas",
                "OK: (dry run) sent"))

        def finish(job):
            self._fill_placeholders()  # already done when About you was saved; this catches a hand-edited .env
            job.say(f"OK: Atlas's and Iris's files use the name {name}")
            job.say("STEP: Starting Atlas and Iris")
            subprocess.Popen(["cmd", "/c", str(SHARED / "Start Crew.cmd")], creationflags=NO_WINDOW)
            if (ROOT / ".git").exists():
                job.say("STEP: Turning on automatic updates")
                code, out = run([sys.executable, str(SHARED / "update.py"), "--install-task"], timeout=30)
                job.say("OK: Atlas checks for updates every 30 minutes" if code == 0
                        else f"WARN: The update task wasn't made: {out.strip()[-200:]}")
            for _ in range(45):  # the watchdogs start the bots; wait for Atlas to hold its lock
                time.sleep(2)
                if (ROOT / "Atlas" / "status.json").exists():
                    break
            time.sleep(10)
            job.say("STEP: Texting you from Atlas")
            py = sys.executable.replace("pythonw.exe", "python.exe")
            code, out = run([py, str(SHARED / "notify.py"), "Atlas",
                             f"Hey {name}, it's Atlas. Setup's done and I'm live. Text me anything."],
                            timeout=90, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            if code:
                job.say(f"ERROR: The text didn't send: {out.strip()[-300:]}")
                return code
            job.say("OK: Sent. Check your phone.")
            return 0
        return self._start("Finish", func=finish)

    def setup_poll(self, job, pos=0):
        j = self._jobs.get(job)
        return j.poll(int(pos or 0)) if j else {"text": "", "pos": 0, "done": True, "code": -1, "results": {}}

    def setup_input(self, job, text):
        j = self._jobs.get(job)
        return bool(j and j.write(str(text)))

    def setup_cancel(self, job):
        j = self._jobs.get(job)
        if j:
            j.cancel()
        return True
