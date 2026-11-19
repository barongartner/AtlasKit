r"""Atlas - the crew's control center, as a normal Windows app.

One window for the whole crew (Atlas and Iris) and the iMessage bridge:
who's running, start / stop / restart, live activity logs, every chat Iris
has, each bot's memory and library, and their settings.

pywebview hosts the page (Atlas UI.html), styled like a Windows 11 app with
the native title bar, Segoe UI and the system accent colour. Every public
method on Api is callable from the page as pywebview.api.name(...).

Closing the window only closes the window. The bots run on their own under
their watchdogs; this app just watches and steers them.

Run with the Python that has the crew's packages; the shortcut setup makes
does that:  pythonw "App\Atlas.pyw" (from the kit's folder). Bots and
watchdogs are started with the same Python this app runs on.
"""

import ctypes
import json
import msvcrt
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import winreg
from ctypes import wintypes
from datetime import date, datetime
from pathlib import Path

APP = Path(__file__).resolve().parent
ROOT = APP.parent                       # the kit's folder, wherever it was installed
UI = APP / "Atlas UI.html"
ICON = APP / "Atlas.ico"
LOCK = APP / "app.lock"
SHARED = ROOT / "Shared"
sys.path.insert(0, str(SHARED))
import owner  # noqa: E402

# The Python this app runs on, windowless, for starting bots and watchdogs.
PYW = Path(sys.executable).with_name("pythonw.exe")
PYW = PYW if PYW.exists() else Path(sys.executable)
PY = Path(sys.executable).with_name("python.exe")
PY = PY if PY.exists() else Path(sys.executable)
PEOPLE = ROOT / "Iris" / "Memories" / "People"
IRIS_ENV = ROOT / "Iris" / ".env"
OWNER_LABEL = f"{owner.NAME} (you)"
_bridge_log: Path | None = None


def bridge_log() -> Path:
    """The bridge's log inside WSL, seen from Windows. BRIDGE_LOG in
    Shared\\.env wins; else WSL_USER's home; else the first home that has one."""
    global _bridge_log
    if _bridge_log is None or not _bridge_log.exists():
        if owner.get("BRIDGE_LOG"):
            _bridge_log = Path(owner.get("BRIDGE_LOG"))
        else:
            home = Path(f"//wsl.localhost/{owner.WSL_DISTRO}/home")
            if owner.get("WSL_USER"):
                _bridge_log = home / owner.get("WSL_USER") / "imessage-bridge" / "bridge.log"
            else:
                try:
                    _bridge_log = next(home.glob("*/imessage-bridge/bridge.log"), None)
                except OSError:
                    _bridge_log = None
                _bridge_log = _bridge_log or home / "imessage-bridge" / "bridge.log"
    return _bridge_log
NO_WINDOW = 0x08000000
# Bots get their own process group and no console, so they outlive this app.
DETACHED = 0x00000008 | 0x00000200 | NO_WINDOW

CREW = [
    {"name": "Atlas", "role": "Headman. Briefs, check-ins, reminders, the calendar."},
    {"name": "Iris", "role": "Social. Texts people and runs the group chats."},
]
NAMES = [b["name"] for b in CREW]
SECRET = re.compile(r"TOKEN|PASSWORD|SESSIONID|API_KEY|SECRET|_KEY$")


# ---------------------------------------------------------------- helpers --

def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_env(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def safe_name(name: str) -> str:
    """Same folder naming Iris uses for People\\<name>."""
    name = Path(str(name).replace("\\", "/")).name
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "File"


OWNER_FOLDER = safe_name(owner.NAME)  # Iris's People folder for the owner


def clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def when(iso) -> str:
    """'3:41 PM', 'Yesterday 9:02 AM' or 'Oct 19, 9:02 AM'."""
    try:
        dt = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return ""
    days = (date.today() - dt.date()).days
    if days == 0:
        return clock(dt)
    if days == 1:
        return f"Yesterday {clock(dt)}"
    if days == -1:
        return f"Tomorrow {clock(dt)}"
    return f"{dt:%b} {dt.day}, {clock(dt)}"


def ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return "just now" if s < 10 else f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def probe_lock(path: Path) -> bool:
    """True while a process holds the single-instance byte lock on path."""
    try:
        fh = open(path, "r+")
    except OSError:
        return False
    try:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return True
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return False
    finally:
        fh.close()


def paths(name: str) -> dict:
    base, low = ROOT / name, name.lower()
    return {"base": base, "script": base / f"{low}_bot.py", "watchdog": base / f"{low}_watchdog.pyw",
            "lock": base / f"{low}.lock", "wlock": base / "watchdog.lock", "stop": base / "stop.flag",
            "status": base / "status.json", "usage": base / "usage.json", "log": base / f"{low}.log",
            "env": base / ".env"}


def pids_of(path: Path) -> list[int]:
    """Processes running exactly this script (by full command line; stored
    pids get recycled by Windows)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
             f"Where-Object {{ $_.CommandLine -like '*{path}*' }} | Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW).stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(t) for t in out.split() if t.isdigit()]


def http_alive(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=1.5)
        return True
    except urllib.error.HTTPError:
        return True   # it answered, even if with a 404
    except Exception:
        return False


def theme() -> dict:
    """Follow Windows: light/dark and the accent colour palette."""
    dark, accent = True, ["#99ebff", "#60cdff", "#0078d4", "#005fb8"]
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        dark = winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        pass
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent")
        pal = winreg.QueryValueEx(k, "AccentPalette")[0]
        cols = ["#%02x%02x%02x" % (pal[i], pal[i + 1], pal[i + 2]) for i in range(0, 32, 4)]
        accent = [cols[0], cols[1], cols[3], cols[4]]  # light3, light2, base, dark1
    except (OSError, IndexError):
        pass
    return {"dark": dark, "accent": accent}


# ------------------------------------------------------------------- logs --

STAMP = re.compile(r"^(\w{3}) (\w{3}) (\d{1,2}), (\d{1,2}):(\d{2}):(\d{2}) ([AP]M)  (.*)$")
NOISE = re.compile(r"telegram\.error|httpx\.(ReadTimeout|ConnectError)|Bad Gateway|NetworkError")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def kind_of(msg: str) -> str:
    if msg.startswith("PROBLEM:"):
        return "problem"
    if msg.startswith("Heads up:"):
        return "warn"
    if re.search(r"\(iMessage\)|\(Telegram\)| replied:|\[\+follow-up\]|^Texted |^Posted in |\(tel:|\(mailto:", msg):
        return "chat"
    if re.search(r"heartbeat|brief|check-out|check-in|review|Starting the|bus sent|^Group ", msg, re.I):
        return "sched"
    return "info"


def parse_log(text: str, first_partial: bool) -> list[dict]:
    """Readable crew log -> entries. A stamped line plus the unstamped lines
    under it (tracebacks, multi-line replies) are one entry."""
    lines = text.splitlines()
    if first_partial and lines:
        lines = lines[1:]
    out: list[dict] = []
    for line in lines:
        m = STAMP.match(line)
        if m:
            _, mon, day, h, mi, s, ap, msg = m.groups()
            out.append({"day": f"{m.group(1)} {mon} {day}", "t": f"{h}:{mi}:{s} {ap}",
                        "kind": kind_of(msg), "text": msg, "detail": ""})
        elif out and line.strip():
            out[-1]["detail"] += line + "\n"
    for e in out:
        if NOISE.search(e["text"] + e["detail"]):
            e["kind"] = "noise"
        elif e["detail"].lstrip().startswith("Traceback") and e["kind"] != "problem":
            e["kind"] = "problem"
    return out


BRIDGE_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})\.\d+[+-]\d{2}:\d{2} (DBG|INF|WRN|ERR|FTL) (.*)$")


def parse_bridge(text: str, first_partial: bool) -> list[dict]:
    lines = ANSI.sub("", text).splitlines()
    if first_partial and lines:
        lines = lines[1:]
    out = []
    for line in lines:
        m = BRIDGE_LINE.match(line)
        if not m or m.group(5) == "DBG":
            continue
        d, h, mi, s, lvl, msg = m.groups()
        hh = int(h)
        dt = datetime.strptime(d, "%Y-%m-%d")
        msg = re.sub(r"\s+\w+=\S+", "", msg).strip() or msg  # drop key=value noise
        kind = {"ERR": "problem", "FTL": "problem", "WRN": "warn"}.get(lvl, "info")
        if "wedge-guard" in msg or "Connected to iMessage" in msg:
            kind = "sched"
        out.append({"day": f"{dt:%a %b} {dt.day}", "t": f"{hh % 12 or 12}:{mi}:{s} {'AM' if hh < 12 else 'PM'}",
                    "kind": kind, "text": msg, "detail": ""})
    return out


# ------------------------------------------------------------------ chats --

CHAT_LINE = re.compile(r"^\*\*(\d{1,2}:\d{2} [AP]M) - (.+?):\*\* ?(.*)$")
_SENT = re.compile(r"\[sent (.+?)(?: [0-9a-f]{4})?(\.\w+)\]")
_RECEIVED = re.compile(r"\[(Voice note|Photo|Video|File) received: [^\]]*\]\s*")
_PLAIN = {"[audio]": "Voice note", "[image]": "Photo", "[video]": "Video", "[file]": "File",
          "[unreadable]": "Sent something the bridge can't read"}


def pretty(text: str) -> str:
    def sent(m):
        ext = m.group(2).lower()
        if ext == ".vcf":
            return "Sent a contact card"
        kind = "a picture" if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp") else "a file"
        return f"Sent {kind}: {m.group(1)}"
    t = _PLAIN.get(text.strip(), text)
    t = _SENT.sub(sent, t)
    t = _RECEIVED.sub(lambda m: m.group(1) + ": ", t)
    t = re.sub(r"^\[Voice note\]\s*", "Voice note: ", t, flags=re.M)
    t = re.sub(r"\[reacted ([^\]]+)\]\s*", r"Reacted \1\n", t)
    t = re.sub(r"^\[edited\]\s*", "Edited: ", t, flags=re.M)
    t = re.sub(r"^On the call: ", "On a call: ", t, flags=re.M)
    return re.sub(r":\s*$", "", t.strip(), flags=re.M)


# -------------------------------------------------------------------- api --

class Api:
    """Everything the page can call. State is underscore-prefixed so
    pywebview doesn't try to expose it."""

    def __init__(self) -> None:
        self._window = None
        self._pending: dict[str, tuple[str, float]] = {}   # bot -> (what, since)
        self._bridge = {"up": None, "synapse": None, "lastIn": "", "lastOut": "", "wedge": False}
        self._chat_cache: dict[str, tuple[float, str]] = {}
        self._rooms: tuple[float, dict] = (0.0, {})          # chat folder -> Matrix rooms
        self._me = ""                                         # the crew's Matrix user
        self._names: dict[str, str] = {}                      # handle -> contact name
        threading.Thread(target=self._bridge_loop, daemon=True).start()
        threading.Thread(target=self._names_loop, daemon=True).start()

    # ----- boot / theme -----

    def boot(self):
        return {"crew": CREW, "theme": theme(), "version": "1.0", "owner": OWNER_FOLDER}

    def theme(self):
        return theme()

    # ----- crew status -----

    def snapshot(self):
        now = datetime.now()
        bots = []
        for b in CREW:
            name = b["name"]
            p = paths(name)
            running = probe_lock(p["lock"])
            st = read_json(p["status"]) if running else {}
            usage = read_json(p["usage"]).get(date.today().isoformat(), {})
            up = ""
            try:
                up = ago((now - datetime.fromisoformat(st["started_at"])).total_seconds())
            except (KeyError, TypeError, ValueError):
                pass
            last = ""
            try:
                last = ago((now - datetime.fromisoformat(read_json(p["status"])["last_activity"])).total_seconds())
            except (KeyError, TypeError, ValueError):
                pass
            pend = self._pending.get(name)
            if pend and (time.time() - pend[1] > 60 or (pend[0] in ("Starting", "Restarting") and running
                                                          and time.time() - pend[1] > 4)
                         or (pend[0] == "Stopping" and not running)):
                self._pending.pop(name, None)
                pend = None
            bots.append({
                "name": name, "running": running, "watchdog": probe_lock(p["wlock"]),
                "onHold": p["stop"].exists(), "busy": bool(st.get("active")), "uptime": up,
                "lastActivity": last, "nextHeartbeat": when(st.get("next_heartbeat")),
                "nextScheduled": st.get("next_scheduled") or "", "turns": usage.get("turns", 0),
                "cost": round(usage.get("cost", 0.0), 2), "pending": pend[0] if pend else "",
                "installed": p["script"].exists(),
            })
        return {"bots": bots, "bridge": dict(self._bridge), "now": clock(now)}

    def _bridge_loop(self):
        while True:
            try:
                b = {"up": http_alive("http://127.0.0.1:29332/"), "synapse": http_alive("http://127.0.0.1:8008/health")}
                tail = self._tail_bytes(bridge_log(), 400_000)
                ins = [m for m in re.finditer(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\S* \S+ [^\n]*RemoteEventMessage", tail)]
                outs = [m for m in re.finditer(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\S* \S+ [^\n]*ID send message", tail)]
                warn = [m for m in re.finditer(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\S* \S+ [^\n]*ZERO IDS notifications", tail)]
                starts = [m for m in re.finditer(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\S* \S+ [^\n]*Starting bridge", tail)]
                last_in = datetime.fromisoformat(ins[-1].group(1)) if ins else None
                last_warn = datetime.fromisoformat(warn[-1].group(1)) if warn else None
                last_start = datetime.fromisoformat(starts[-1].group(1)) if starts else None
                b["lastIn"] = ago((datetime.now() - last_in).total_seconds()) if last_in else ""
                b["lastOut"] = ago((datetime.now() - datetime.fromisoformat(outs[-1].group(1))).total_seconds()) if outs else ""
                # Only a warning from the CURRENT bridge process counts. The bridge
                # logs "ZERO IDS" after any quiet hour, and wedge-guard restarts it
                # within 10 min; counting older warnings would keep showing "Not
                # receiving" after that restart until the next text came in.
                b["wedge"] = bool(last_warn and (not last_in or last_warn > last_in)
                                  and (not last_start or last_warn > last_start))
                self._bridge.update(b)
            except Exception:
                pass
            time.sleep(10)

    @staticmethod
    def _tail_bytes(path: Path, n: int) -> str:
        try:
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - n))
                return ANSI.sub("", fh.read().decode("utf-8", "replace"))
        except OSError:
            return ""

    # ----- controls -----

    def control(self, name, action):
        """Start / Stop / Restart a bot, or restart the bridge. Runs in the
        background; the page sees progress through snapshot()."""
        if name == "Bridge":
            self._pending["Bridge"] = ("Restarting", time.time())
            threading.Thread(target=self._restart_bridge, daemon=True).start()
            return True
        if name not in NAMES:
            return False
        label = {"start": "Starting", "stop": "Stopping", "restart": "Restarting"}[action]
        self._pending[name] = (label, time.time())
        threading.Thread(target=self._control, args=(name, action), daemon=True).start()
        return True

    def _control(self, name, action):
        p = paths(name)
        try:
            if action in ("stop", "restart"):
                if action == "stop":
                    p["stop"].write_text("stop", encoding="utf-8")  # the watchdog stands down
                for pid in pids_of(p["script"]):
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True, creationflags=NO_WINDOW)
                time.sleep(1.5)
            if action in ("start", "restart"):
                p["stop"].unlink(missing_ok=True)
                py = str(PYW)
                env = self._bot_env(p["env"])
                if not probe_lock(p["lock"]):
                    subprocess.Popen([py, str(p["script"])], cwd=p["base"], creationflags=DETACHED, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
                if not probe_lock(p["wlock"]) and p["watchdog"].exists():
                    subprocess.Popen([py, str(p["watchdog"])], cwd=p["base"], creationflags=DETACHED, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        except Exception as e:  # noqa: BLE001
            self._toast(f"{name}: {action} failed ({e})", True)

    @staticmethod
    def _bot_env(env_file: Path) -> dict:
        """This app's environment minus anything that would beat the bot's own
        .env. The bots load it without override, so an inherited CLAUDE_EFFORT
        (or any key the .env sets) would silently win. Started from a Claude
        session, this app carries a pile of CLAUDE_* variables; none of them
        belong in a bot."""
        own = set(read_env(env_file))
        return {k: v for k, v in os.environ.items()
                if k not in own and not k.upper().startswith(("CLAUDE", "ANTHROPIC"))}

    def _restart_bridge(self):
        try:
            subprocess.run(["wsl", "-d", owner.WSL_DISTRO, "-u", "root", "--", "pkill", "-9", "-f", "matrix-rustpush"],
                           capture_output=True, timeout=30, creationflags=NO_WINDOW)
            time.sleep(2)
            subprocess.run(["wsl", "-d", owner.WSL_DISTRO, "--", "bash", "-lc",
                            "${BRIDGE_DIR:-$HOME/imessage-bridge}/start.sh"],
                           capture_output=True, timeout=60, creationflags=NO_WINDOW)
            for _ in range(20):
                time.sleep(1.5)
                if http_alive("http://127.0.0.1:29332/"):
                    break
            self._bridge["up"] = http_alive("http://127.0.0.1:29332/")
            self._toast("Bridge restarted" if self._bridge["up"] else "Bridge didn't come back up", not self._bridge["up"])
        finally:
            self._pending.pop("Bridge", None)

    def _toast(self, text, error=False):
        if self._window:
            try:
                self._window.evaluate_js(f"toast({json.dumps(text)}, {json.dumps(error)})")
            except Exception:
                pass

    # ----- logs -----

    def log_tail(self, name, pos):
        """New log entries since byte pos (-1 = start near the end)."""
        path = bridge_log() if name == "Bridge" else paths(name)["log"]
        try:
            size = path.stat().st_size
        except OSError:
            return {"pos": 0, "entries": [], "reset": True, "missing": True}
        reset = pos < 0 or pos > size
        start = max(0, size - (400_000 if name == "Bridge" else 160_000)) if reset else pos
        if start == size and not reset:
            return {"pos": size, "entries": [], "reset": False}
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                raw = fh.read(size - start)
        except OSError:
            return {"pos": pos, "entries": [], "reset": False}
        # Only hand over complete lines; the rest waits for the next poll.
        cut = raw.rfind(b"\n")
        if cut < 0:
            return {"pos": start, "entries": [], "reset": reset}
        text = raw[:cut + 1].decode("utf-8", "replace")
        parse = parse_bridge if name == "Bridge" else parse_log
        return {"pos": start + cut + 1, "entries": parse(text, reset and start > 0)[-1500:], "reset": reset}

    def open_log(self, name):
        path = bridge_log() if name == "Bridge" else paths(name)["log"]
        self._open(path)

    # ----- chats -----

    def _chat_text(self, f: Path) -> str:
        try:
            mt = f.stat().st_mtime
        except OSError:
            return ""
        hit = self._chat_cache.get(str(f))
        if not hit or hit[0] != mt:
            try:
                hit = (mt, f.read_text(encoding="utf-8"))
            except OSError:
                hit = (mt, "")
            self._chat_cache[str(f)] = hit
        return hit[1]

    def chats(self, query=""):
        q = (query or "").strip().lower()
        out = []
        try:
            folders = [d for d in PEOPLE.iterdir() if d.is_dir()]
        except OSError:
            folders = []
        for d in folders:
            days = sorted(d.glob("*.md"))
            if not days:
                continue
            last = days[-1]
            body = self._chat_text(last)
            lines = [m for m in (CHAT_LINE.match(l) for l in body.splitlines()) if m]
            if q and q not in d.name.lower() and not any(q in self._chat_text(f).lower() for f in days):
                continue
            mt = max(f.stat().st_mtime for f in days)
            preview = ""
            if lines:
                who, txt = lines[-1].group(2), pretty(lines[-1].group(3))
                preview = ("You: " if who == "Iris" else ("" if d.name.startswith("Group ") is False else f"{who}: ")) + txt
            out.append({"id": d.name, "name": OWNER_LABEL if d.name == OWNER_FOLDER else d.name,
                        "group": d.name.startswith("Group "), "when": when(datetime.fromtimestamp(mt).isoformat()),
                        "mtime": mt, "preview": preview[:90], "fresh": time.time() - mt < 600})
        out.sort(key=lambda c: -c["mtime"])
        return out

    def chat(self, cid):
        d = PEOPLE / cid
        if not d.is_dir() or d.parent != PEOPLE:
            return {"messages": [], "sig": 0}
        days = sorted(d.glob("*.md"))
        sig = max((f.stat().st_mtime for f in days), default=0)
        msgs = []
        for f in days:
            try:
                dt = datetime.strptime(f.stem, "%Y-%m-%d")
                label = "Today" if dt.date() == date.today() else f"{dt:%A, %B} {dt.day}"
            except ValueError:
                label = f.stem
            cur = None
            for line in self._chat_text(f).splitlines():
                m = CHAT_LINE.match(line)
                if m:
                    cur = {"day": label, "date": f.stem, "t": m.group(1), "who": m.group(2), "text": m.group(3)}
                    msgs.append(cur)
                elif cur is not None and not line.startswith("# "):
                    cur["text"] += "\n" + line
        for m in msgs:
            m["text"] = pretty(m["text"])
        return {"messages": msgs, "sig": sig, "days": len(days),
                "name": OWNER_LABEL if cid == OWNER_FOLDER else cid, "group": cid.startswith("Group ")}

    def chat_sig(self, cid):
        d = PEOPLE / cid
        try:
            return max(f.stat().st_mtime for f in d.glob("*.md"))
        except (OSError, ValueError):
            return 0

    def reveal_chat(self, cid):
        d = PEOPLE / cid
        if d.is_dir() and d.parent == PEOPLE:
            subprocess.Popen(["explorer", str(d)])

    # ----- email -----
    # Three boxes: Iris's inbox and sent mail (her own account), and the
    # owner's Gmail as Atlas watches it. Plain IMAP with the bots' own .env creds,
    # cached for a minute so flipping between boxes is instant.

    _MAIL_BOXES = {
        "iris_in": ("Iris", "IRIS_EMAIL_ADDRESS", "IRIS_EMAIL_APP_PASSWORD", "INBOX"),
        "iris_sent": ("Iris", "IRIS_EMAIL_ADDRESS", "IRIS_EMAIL_APP_PASSWORD", '"[Gmail]/Sent Mail"'),
        "owner": ("Atlas", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "INBOX"),
    }

    def _mail_login(self, box):
        import imaplib
        bot, addr_key, pw_key, folder = self._MAIL_BOXES[box]
        env = {}
        try:
            for line in (ROOT / bot / ".env").read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"')
        except OSError:
            pass
        addr, pw = env.get(addr_key, ""), env.get(pw_key, "").replace(" ", "")
        if not addr or not pw:
            return None, f"{bot}'s email isn't set up in {bot}\\.env"
        imap = imaplib.IMAP4_SSL("imap.gmail.com", timeout=20)
        imap.login(addr, pw)
        imap.select(folder, readonly=True)
        return imap, ""

    def emails(self, box, fresh=False):
        import email as _email
        from email.header import decode_header, make_header
        from email.utils import parsedate_to_datetime
        if box not in self._MAIL_BOXES:
            return {"items": [], "error": "unknown box"}
        cache = getattr(self, "_mail_cache", {})
        self._mail_cache = cache
        hit = cache.get(box)
        if hit and not fresh and time.time() - hit[0] < 60:
            return hit[1]
        dec = lambda v: str(make_header(decode_header(v or "")))
        watches = []
        if box == "owner":
            try:
                sys.path.insert(0, str(ROOT / "Atlas"))
                import email_watch
                watches = email_watch.load_watches()
            except Exception:  # noqa: BLE001
                watches = []
        items, err = [], ""
        try:
            imap, err = self._mail_login(box)
            if imap:
                _, data = imap.uid("search", None, "ALL")
                uids = data[0].split()[-40:][::-1]
                for uid in uids:
                    _, msg = imap.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
                    h = _email.message_from_bytes(msg[0][1])
                    frm, to, subj = dec(h["From"]), dec(h["To"]), dec(h["Subject"]) or "(no subject)"
                    try:
                        dt = parsedate_to_datetime(h["Date"]).astimezone().replace(tzinfo=None)
                    except (TypeError, ValueError):
                        dt = None
                    label = next((lbl for field, needle, lbl in watches
                                  if needle in (frm if field == "from" else subj).lower()), "")
                    items.append({"uid": uid.decode(), "from": frm, "to": to, "subject": subj,
                                  "when": when(dt.isoformat()) if dt else "",
                                  "fresh": bool(dt and (datetime.now() - dt).total_seconds() < 3600),
                                  "watch": label})
                imap.logout()
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"[:160]
        out = {"items": items, "error": err}
        cache[box] = (time.time(), out)
        return out

    def email_body(self, box, uid):
        import email as _email
        import email.policy
        try:
            imap, err = self._mail_login(box)
            if not imap:
                return {"error": err}
            _, msg = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
            imap.logout()
            m = _email.message_from_bytes(msg[0][1], policy=email.policy.default)
            part = m.get_body(preferencelist=("plain", "html"))
            body = part.get_content() if part else ""
            if part is not None and part.get_content_type() == "text/html":
                body = re.sub(r"<(br|/p|/div|/tr)[^>]*>", "\n", body, flags=re.I)
                body = re.sub(r"<[^>]+>", "", body)
                body = re.sub(r"\n\s*\n\s*\n+", "\n\n", body)
            files = [p.get_filename() for p in m.iter_attachments() if p.get_filename()]
            return {"from": str(m["From"] or ""), "to": str(m["To"] or ""), "cc": str(m["Cc"] or ""),
                    "date": str(m["Date"] or ""), "subject": str(m["Subject"] or ""),
                    "body": body.strip()[:20000], "files": files}
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"[:160]}

    def email_status(self):
        """Is each bot's live mail connection up, and when did mail last push in."""
        def scan(bot, on_pat, push_pat):
            on, last, dropped = False, "", ""
            try:
                with open(ROOT / bot / f"{bot.lower()}.log", "rb") as f:
                    f.seek(0, 2)
                    f.seek(max(0, f.tell() - 400_000))
                    lines = f.read().decode("utf-8", "replace").splitlines()
            except OSError:
                return {"on": False, "last": "", "dropped": ""}
            stamp = lambda l: re.sub(r":\d\d (AM|PM)", r" \1", l.split("  ")[0])  # "Tue Nov 3, 4:42 PM"
            for line in lines:
                if on_pat in line:
                    on, dropped = True, ""
                elif "push connection dropped" in line:
                    dropped = stamp(line)
                elif push_pat in line:
                    last = stamp(line)
            return {"on": on, "last": last, "dropped": dropped}
        return {"iris": scan("Iris", "Email push on", "Starting the email"),
                "atlas": scan("Atlas", "Gmail push on", "Gmail pushed new mail")}

    # ----- read receipts -----

    def receipts(self, cid):
        """The crew's recent texts in one of Iris's chats and who's read each,
        straight from Synapse. The page lines them up with the chat log by
        minute and text. The bridge doesn't pass "delivered" through, and
        someone with read receipts off just never shows as read."""
        d = PEOPLE / str(cid)
        if not d.is_dir() or d.parent != PEOPLE:
            return {"ok": False, "events": []}
        try:
            return self._receipts(str(cid))
        except Exception as e:  # noqa: BLE001  Synapse down, bridge restarting...
            return {"ok": False, "events": [], "error": str(e)[:200]}

    def _mx_get(self, env: dict, path: str, params: dict | None = None):
        from urllib.parse import urlencode
        base = (env.get("MATRIX_URL") or "http://127.0.0.1:8008").rstrip("/")
        url = f"{base}/_matrix/client/v3{path}" + (f"?{urlencode(params)}" if params else "")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {env.get('MATRIX_TOKEN', '')}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def _receipts(self, cid: str) -> dict:
        from urllib.parse import quote
        if str(SHARED) not in sys.path:
            sys.path.insert(0, str(SHARED))
        import imessage as im   # just the handle helpers; it doesn't touch os.environ
        env = read_env(IRIS_ENV)
        for key in ("MATRIX_TOKEN", "MATRIX_URL", "IMESSAGE_HOME_ROOM"):
            env[key] = env.get(key) or owner.get(key)  # Shared\.env holds the shared ones
        if not env.get("MATRIX_TOKEN"):
            return {"ok": False, "events": []}
        owners = {im.normalize_handle(h) or h.strip().lower()
                  for h in (env.get("IMESSAGE_OWNER") or owner.PHONE).split(",") if h.strip()}
        if not self._me:
            self._me = self._mx_get(env, "/account/whoami").get("user_id", "")
        known = read_json(PEOPLE / "Known.json")
        if time.time() - self._rooms[0] > 300:
            groups = read_json(PEOPLE / "Groups.json")
            found: dict[str, list[str]] = {}
            for room in self._mx_get(env, "/joined_rooms").get("joined_rooms", []):
                try:
                    st = self._mx_get(env, f"/rooms/{quote(room)}/state/m.bridge/" + quote(im.BRIDGE_STATE_KEY, safe=""))
                except Exception:  # noqa: BLE001  not a bridge room
                    continue
                pid = (st.get("channel") or {}).get("id", "")
                peer = im.split_portal_id(pid)[0]
                if peer.startswith("gid:"):
                    folder = groups.get(pid) or groups.get(peer)
                else:
                    h = im.normalize_handle(peer)
                    folder = OWNER_FOLDER if h and h.lower() in owners else known.get(h)
                if folder:
                    found.setdefault(safe_name(folder), []).append(room)
            home = env.get("IMESSAGE_HOME_ROOM", "")
            if home and home not in found.get(OWNER_FOLDER, []):
                found.setdefault(OWNER_FOLDER, []).append(home)
            self._rooms = (time.time(), found)
        mine = cid == OWNER_FOLDER   # in the owner's own chat it's their reads that count
        events = []
        for room in self._rooms[1].get(cid, []):
            flt = {"room": {"rooms": [room], "timeline": {"limit": 80}, "ephemeral": {"types": ["m.receipt"]},
                            "state": {"types": []}}, "presence": {"types": []}, "account_data": {"types": []}}
            data = self._mx_get(env, "/sync", {"timeout": "0", "filter": json.dumps(flt)})
            r = data.get("rooms", {}).get("join", {}).get(room, {})
            timeline = r.get("timeline", {}).get("events", [])
            order = {ev.get("event_id"): i for i, ev in enumerate(timeline)}
            upto: dict[str, tuple[int, int]] = {}   # reader -> (read up to this index, when)
            for ev in r.get("ephemeral", {}).get("events", []):
                for eid, kinds in (ev.get("content") or {}).items():
                    for user, info in ((kinds or {}).get("m.read") or {}).items():
                        if user == self._me or not user.startswith(im.GHOST_PREFIX):
                            continue
                        if (im.decode_ghost(user).lower() in owners) != mine:
                            continue
                        pos = order.get(eid, -1)
                        if pos > upto.get(user, (-1, 0))[0]:
                            upto[user] = (pos, int(info.get("ts") or 0))
            for i, ev in enumerate(timeline):
                if ev.get("sender") != self._me or ev.get("type") != "m.room.message":
                    continue
                c = ev.get("content") or {}
                sent = datetime.fromtimestamp(ev.get("origin_server_ts", 0) / 1000)
                readers = []
                for user, (pos, ts) in sorted(upto.items(), key=lambda kv: kv[1][1]):
                    if pos >= i:
                        h = im.decode_ghost(user)
                        name = cid if not cid.startswith("Group ") else (
                            self._names.get(h) or known.get(h) or h.split(":", 1)[-1])
                        readers.append({"name": name, "at": when(datetime.fromtimestamp(ts / 1000).isoformat()) if ts else ""})
                events.append({"ts": ev.get("origin_server_ts", 0), "date": f"{sent:%Y-%m-%d}",
                               "min": sent.hour * 60 + sent.minute, "t": clock(sent),
                               "body": str(c.get("body", ""))[:200], "media": c.get("msgtype") != "m.text",
                               "readers": readers})
        events.sort(key=lambda e: e["ts"])
        return {"ok": True, "events": events[-120:], "group": cid.startswith("Group ")}

    def _names_loop(self):
        """Contact names for group read receipts. Runs receipts.py's lookup in
        its own process: contacts.py loads Atlas's .env into os.environ, and
        anything in this process's environment would leak into bots it starts."""
        py = PY
        code = ("import json, sys; sys.path.insert(0, r'%s'); import receipts; "
                "sys.stdout.reconfigure(encoding='utf-8'); print(json.dumps(receipts.names_by_handle()))") % SHARED
        while True:
            try:
                out = subprocess.run([str(py), "-c", code], cwd=str(SHARED), capture_output=True, text=True,
                                     encoding="utf-8", timeout=120, creationflags=NO_WINDOW,
                                     env=self._bot_env(Path("nul"))).stdout
                names = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
                if names:
                    self._names = {k: v for k, v in names.items() if "|" not in k}
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1800)

    # ----- memory / library -----

    def files(self, name):
        if name not in NAMES:
            return []
        base = ROOT / name
        found = []
        for rel in ["CLAUDE.md"]:
            if (base / rel).exists():
                found.append(base / rel)
        found += sorted((base / "Memories").glob("*.md"))
        found += sorted((base / "Library").rglob("*.md")) if (base / "Library").exists() else []
        out = []
        for f in found:
            try:
                st = f.stat()
            except OSError:
                continue
            rel = f.relative_to(base).as_posix()
            group = "Instructions" if rel == "CLAUDE.md" else ("Memories" if rel.startswith("Memories/") else "Library")
            out.append({"rel": rel, "name": f.stem, "group": group, "size": st.st_size,
                        "when": when(datetime.fromtimestamp(st.st_mtime).isoformat())})
        return out

    def _safe(self, name, rel) -> Path | None:
        if name not in NAMES:
            return None
        base = (ROOT / name).resolve()
        p = (base / rel).resolve()
        return p if base in p.parents and p.is_file() else None

    def read_file(self, name, rel):
        p = self._safe(name, rel)
        if not p:
            return {"text": "", "error": "Not found"}
        data = p.read_bytes()
        cut = len(data) > 600_000
        text = data[-600_000:].decode("utf-8", "replace") if cut else data.decode("utf-8", "replace")
        return {"text": text, "truncated": cut, "size": len(data)}

    def open_file(self, name, rel):
        p = self._safe(name, rel)
        if p:
            self._open(p)

    def reveal_file(self, name, rel):
        p = self._safe(name, rel)
        if p:
            subprocess.Popen(["explorer", "/select,", str(p)])

    def open_folder(self, name):
        if name in NAMES:
            subprocess.Popen(["explorer", str(ROOT / name)])

    @staticmethod
    def _open(path: Path):
        try:
            os.startfile(str(path))
        except OSError:
            pass

    # ----- settings (.env) -----

    def env(self, name):
        if name not in NAMES:
            return []
        out = []
        try:
            lines = paths(name)["env"].read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                out.append({"key": k.strip(), "value": v.strip(), "secret": bool(SECRET.search(k.strip()))})
        return out

    def save_env(self, name, values):
        """values: {key: value}. Keeps comments and order; new keys go last."""
        if name not in NAMES or not isinstance(values, dict):
            return False
        path = paths(name)["env"]
        try:
            raw = path.read_bytes().decode("utf-8")
        except OSError:
            raw = ""
        lines = raw.splitlines()
        nl = "\r\n" if "\r\n" in raw else "\n"   # keep the file's own line endings
        seen = set()
        for i, line in enumerate(lines):
            if "=" in line and not line.lstrip().startswith("#"):
                k = line.partition("=")[0].strip()
                if k in values:
                    lines[i] = f"{k}={str(values[k]).strip()}"
                    seen.add(k)
        lines += [f"{k}={str(v).strip()}" for k, v in values.items() if k not in seen and k.strip()]
        path.write_bytes((nl.join(lines) + nl).encode("utf-8"))
        return True

    # ----- window -----

    def window(self, what):
        if what == "minimize" and self._window:
            self._window.minimize()


# ------------------------------------------------------------------- main --

def style_window(api: Api):
    """Dark native title bar to match the page when Windows is dark, plus the
    app icon. WinForms calls have to happen on its UI thread."""
    try:
        from System import Action
        from System.Drawing import Icon
        form = api._window.native

        def setup():
            hwnd = form.Handle.ToInt64()
            on = ctypes.c_int(1 if theme()["dark"] else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), 20, ctypes.byref(on), 4)
            if ICON.exists():
                form.Icon = Icon(str(ICON))
            # Nudge the frame so the new title bar colour paints right away.
            ctypes.windll.user32.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020)
        form.Invoke(Action(setup))
    except Exception:
        pass


def already_running() -> bool:
    global _lock_fh
    _lock_fh = open(LOCK, "w")
    try:
        msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
        return False
    except OSError:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, "Atlas")
        if hwnd:
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
        return True


def main():
    if already_running():
        return
    import shutil
    import webview
    # pywebview serves the page over a local http server, so WebView2's disk
    # cache can hand back an old Atlas UI.html after an update, so a new page
    # wouldn't show. Drop the cache every start.
    for d in ("Cache", "Code Cache"):
        shutil.rmtree(APP / "WebView Data" / "EBWebView" / "Default" / d, ignore_errors=True)
    api = Api()
    t = theme()
    api._window = webview.create_window("Atlas", str(UI), js_api=api, width=1320, height=860,
                                        min_size=(980, 640), background_color="#202020" if t["dark"] else "#f3f3f3")
    api._window.events.shown += lambda: style_window(api)
    webview.start(private_mode=False, storage_path=str(APP / "WebView Data"))


if __name__ == "__main__":
    main()
