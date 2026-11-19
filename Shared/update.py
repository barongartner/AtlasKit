r"""Keeps this Atlas install on the latest code from GitHub.

The "Atlas Update" scheduled task runs this every 30 minutes (setup makes the
task, or `update.py --install-task`). Each run:

1. git fetch. Nothing new on origin/main means it's done.
2. Waits for Atlas and Iris to be idle (status.json "active" is 0), so no
   reply gets cut off. Still busy after 20 minutes means skip; the next run
   tries again.
3. Saves every file the bots or setup changed (Library, Memories, filled-in
   CLAUDE.md, anything else) to Shared\update-backup\<time>\, moves the code
   to origin/main, then puts the Library and Memories files back as they were.
   CLAUDE.md is filled in again from the new version with the owner's name
   and picks. Secrets and runtime files are gitignored, so git never touches
   them.
4. Reinstalls the Python packages when requirements.txt changed.
5. Restarts the crew quietly: watchdogs first (so nobody gets a "went down"
   text), then the bots, then Start Crew.cmd. Reopens the Atlas app if it was
   open.

Every step goes in Shared\update.log, and the last result in
Shared\update.json.

  update.py               check and update (what the task runs)
  update.py --now         start one right away (through the task, safe mid-turn)
  update.py --check       only say whether an update is waiting
  update.py --restart     restart the crew even with nothing new
  update.py --install-task  make the scheduled task
"""

import json
import msvcrt
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SHARED = Path(__file__).resolve().parent
ROOT = SHARED.parent
LOG = SHARED / "update.log"
STATE = SHARED / "update.json"
LOCK = SHARED / "update.lock"
BACKUPS = SHARED / "update-backup"
BRANCH = "main"
BOTS = ("Atlas", "Iris")
IDLE_WAIT = 20 * 60
NO_WINDOW = 0x08000000
DETACHED = 0x00000008
NEW_GROUP = 0x00000200
TASK = "Atlas Update"
# The bots' own files: put back exactly as they were after every update.
KEEP_DIRS = ("Atlas/Library/", "Atlas/Memories/", "Iris/Memories/")


def log(text: str) -> None:
    t = datetime.now()
    stamp = f"{t:%a %b} {t.day}, {t.strftime('%I').lstrip('0')}:{t:%M:%S %p}"
    try:
        if LOG.exists() and LOG.stat().st_size > 300_000:
            LOG.write_text(LOG.read_text(encoding="utf-8", errors="replace")[-150_000:], encoding="utf-8")
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp}  {text}\n")
    except OSError:
        pass
    if sys.stdout and not sys.executable.lower().endswith("pythonw.exe"):
        try:
            print(text, flush=True)
        except (OSError, ValueError):
            pass


def save_state(**kw) -> None:
    try:
        st = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    except (OSError, ValueError):
        st = {}
    st.update(kw)
    st["checked"] = datetime.now().isoformat(timespec="seconds")
    try:
        STATE.write_text(json.dumps(st, indent=1), encoding="utf-8")
    except OSError:
        pass


def git_exe() -> str:
    found = shutil.which("git")
    if found:
        return found
    for p in (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files (x86)\Git\cmd\git.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\cmd\git.exe")):
        if Path(p).exists():
            return p
    raise SystemExit("git isn't installed")


def git(*args, check=True) -> str:
    r = subprocess.run([git_exe(), "-C", str(ROOT), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=300)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[-400:]}")
    return r.stdout.strip()


def python_exe(windowless=False) -> str:
    exe = Path(sys.executable)
    want = "pythonw.exe" if windowless else "python.exe"
    other = exe.with_name(want)
    return str(other if other.exists() else exe)


# ----- the crew's processes -----

def procs() -> list[dict]:
    """Every python process with its command line (CIM, no window)."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
          "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, creationflags=NO_WINDOW, timeout=60)
        data = json.loads(r.stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    return data if isinstance(data, list) else [data]


def script_of(cmdline: str) -> Path | None:
    """The .py/.pyw file a python command line runs, resolved to a real path."""
    import shlex
    try:
        parts = shlex.split(cmdline or "", posix=False)
    except ValueError:
        parts = (cmdline or "").split()
    for p in parts[1:]:
        p = p.strip('"')
        if p.lower().endswith((".py", ".pyw")):
            try:
                return Path(os.path.normpath(p)).resolve()
            except OSError:
                return None
    return None


def find(paths: list[Path]) -> list[tuple[int, Path]]:
    want = {str(p.resolve()).lower() for p in paths}
    out = []
    for pr in procs():
        s = script_of(pr.get("CommandLine") or "")
        if s and str(s).lower() in want:
            out.append((int(pr["ProcessId"]), s))
    return out


def kill(pids) -> None:
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True,
                       creationflags=NO_WINDOW, timeout=30)


def busy_bots() -> list[str]:
    busy = []
    for b in BOTS:
        try:
            st = json.loads((ROOT / b / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if st.get("active"):
            busy.append(b)
    return busy


def wait_idle() -> bool:
    end = time.time() + IDLE_WAIT
    while True:
        busy = busy_bots()
        if not busy:
            return True
        if time.time() > end:
            log(f"{' and '.join(busy)} still mid-reply after {IDLE_WAIT // 60} minutes; trying again next run")
            return False
        time.sleep(10)


def stop_crew() -> bool:
    """Watchdogs first, then the bots. Returns whether the app was open."""
    dogs = [ROOT / b / f"{b.lower()}_watchdog.pyw" for b in BOTS]
    bots = [ROOT / b / f"{b.lower()}_bot.py" for b in BOTS]
    app = [ROOT / "App" / "Atlas.pyw"]
    found_dogs = find(dogs)
    kill(pid for pid, _ in found_dogs)
    time.sleep(2)
    found_bots = find(bots)
    kill(pid for pid, _ in found_bots)
    found_app = find(app)
    kill(pid for pid, _ in found_app)
    log(f"stopped {len(found_dogs)} watchdog(s), {len(found_bots)} bot(s)"
        + (", the Atlas app" if found_app else ""))
    time.sleep(3)
    return bool(found_app)


def start_crew(app_was_open: bool) -> None:
    flags = DETACHED | NEW_GROUP | NO_WINDOW
    # A bot with a stop.flag stays down: its watchdog respects the flag.
    subprocess.Popen(["cmd", "/c", str(SHARED / "Start Crew.cmd")], cwd=str(SHARED), creationflags=flags)
    if app_was_open:
        subprocess.Popen([python_exe(windowless=True), str(ROOT / "App" / "Atlas.pyw")],
                         cwd=str(ROOT / "App"), creationflags=DETACHED | NEW_GROUP)
    log("crew started again" + (" and the app reopened" if app_was_open else ""))


# ----- the update itself -----

def changed_files() -> list[str]:
    """Tracked files that differ from HEAD on disk."""
    out = git("status", "--porcelain", "-uno", "-z")
    files = []
    for entry in out.split("\0"):
        if len(entry) > 3:
            files.append(entry[3:])
    return files


def refill_claude_md() -> None:
    """Fill the owner's name and picks into the new CLAUDE.md files."""
    for b in BOTS:
        (ROOT / b / "CLAUDE.template.md").unlink(missing_ok=True)
    sys.path.insert(0, str(ROOT / "App"))
    sys.path.insert(0, str(SHARED))
    try:
        import setup_flow
        done = setup_flow.SetupMixin()._fill_placeholders()
        log(f"filled in the owner's name again ({len(done)} file(s))")
    except Exception as e:  # noqa: BLE001
        log(f"WARNING: couldn't fill in CLAUDE.md: {e!r}")


def update(force_restart=False) -> int:
    git("fetch", "--quiet", "origin", BRANCH)
    head = git("rev-parse", "HEAD")
    new = git("rev-parse", f"origin/{BRANCH}")
    if head == new and not force_restart:
        save_state(result="up to date", version=head[:7])
        return 0
    incoming = git("log", "--format=%h %s", f"{head}..{new}").splitlines() if head != new else []
    log(f"update waiting: {head[:7]} -> {new[:7]} ({len(incoming)} commit(s))")
    for line in incoming[:20]:
        log(f"  {line}")
    if not wait_idle():
        save_state(result="waiting for the bots to go idle", version=head[:7], waiting=new[:7])
        return 0

    # Back up everything git might touch.
    stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
    bdir = BACKUPS / stamp
    mine = changed_files()
    added = set(git("diff", "--name-only", "--diff-filter=A", head, new).splitlines()) if head != new else set()
    clash = [f for f in added if (ROOT / f).exists()]
    for f in set(mine) | set(clash):
        src = ROOT / f
        if src.is_file():
            dst = bdir / f
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    if mine or clash:
        log(f"backed up {len(set(mine) | set(clash))} changed file(s) to {bdir}")
    code_changed = [f for f in mine if not f.startswith(KEEP_DIRS) and not f.endswith("CLAUDE.md")]
    if code_changed:
        log("WARNING: these code files had local changes and get replaced (copies in the backup): "
            + ", ".join(code_changed))

    req_before = (ROOT / "requirements.txt").read_bytes() if (ROOT / "requirements.txt").exists() else b""
    im_before = git("rev-parse", "HEAD:Shared/iMessage", check=False)

    app_was_open = stop_crew()
    try:
        git("reset", "--hard", "--quiet", new)
        for f in set(mine) | set(clash):
            if f.startswith(KEEP_DIRS) and (bdir / f).is_file():
                (ROOT / f).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bdir / f, ROOT / f)
        refill_claude_md()

        req_after = (ROOT / "requirements.txt").read_bytes() if (ROOT / "requirements.txt").exists() else b""
        if req_after != req_before:
            log("requirements.txt changed: installing packages")
            r = subprocess.run([python_exe(), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                                "-r", str(ROOT / "requirements.txt")], capture_output=True, text=True,
                               creationflags=NO_WINDOW, timeout=1800)
            log("packages installed" if r.returncode == 0 else f"WARNING: pip failed: {(r.stderr or '')[-400:]}")
        bridge_note = ""
        if git("rev-parse", "HEAD:Shared/iMessage", check=False) != im_before:
            bridge_note = "the iMessage bridge files changed; open the Atlas app's Setup to rebuild the bridge"
            log(f"NOTE: {bridge_note}")
    finally:
        start_crew(app_was_open)
    save_state(result="updated", version=new[:7], previous=head[:7], updated=datetime.now().isoformat(timespec="seconds"),
               commits=incoming[:20], bridge=bridge_note or None, waiting=None)
    log(f"updated to {new[:7]}")
    return 0


def say(text: str) -> None:
    """print() that's safe under pythonw (no stdout there)."""
    if sys.stdout:
        try:
            print(text, flush=True)
        except (OSError, ValueError):
            pass


def install_task() -> int:
    pyw = python_exe(windowless=True)
    tr = f'"{pyw}" "{Path(__file__).resolve()}"'
    r = subprocess.run(["schtasks", "/create", "/f", "/tn", TASK, "/sc", "minute", "/mo", "30", "/tr", tr],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    say((r.stdout or r.stderr).strip())
    log(f"scheduled task {TASK}: " + ("made" if r.returncode == 0 else (r.stderr or r.stdout).strip()))
    return r.returncode


def update_now() -> int:
    """Start an update outside this process tree, so a bot can ask for one
    mid-turn: the task runs it, it waits for the turn to end, then restarts."""
    r = subprocess.run(["schtasks", "/run", "/tn", TASK], capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        subprocess.Popen([python_exe(windowless=True), str(Path(__file__).resolve())], cwd=str(SHARED),
                         creationflags=DETACHED | NEW_GROUP | NO_WINDOW | 0x01000000)  # breakaway from job
    say("Update started. If there's anything new, the crew restarts within a minute or two of this reply.")
    return 0


def main() -> int:
    if "--install-task" in sys.argv:
        return install_task()
    if "--now" in sys.argv:
        return update_now()
    if "--check" in sys.argv:
        git("fetch", "--quiet", "origin", BRANCH)
        head, new = git("rev-parse", "--short", "HEAD"), git("rev-parse", "--short", f"origin/{BRANCH}")
        say("up to date" if head == new else f"update waiting: {head} -> {new}")
        return 0
    LOCK.touch(exist_ok=True)
    fh = LOCK.open("r+")
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return 0  # another run is already going
    try:
        return update(force_restart="--restart" in sys.argv)
    except Exception as e:  # noqa: BLE001
        log(f"ERROR: {e}")
        save_state(result=f"failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
