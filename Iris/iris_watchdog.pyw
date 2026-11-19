"""Iris watchdog - keeps the bot alive and tells the owner when it revives it.

Started at logon (setup adds it) and by Shared\\Start Crew.cmd. Checks
every 60s. Respects stop.flag (intentional stop = stay down until started
again). What it did and why goes in watchdog.log next to this file.

"Is the bot alive" is the bot's own single-instance lock: it holds byte 0 of
iris.lock for as long as its process exists, and Windows drops the lock
the instant the process ends. That replaced reading the pid out of
status.json and asking PowerShell about it: a read that landed mid-write, or
a query that timed out right after the PC woke, looked like a dead bot, so a
watchdog would start a second copy and text "went down" about a bot that
never went anywhere.

A bot can also be alive and stuck. It rewrites status.json every minute, so a
file that stays old while the lock is held means the bot froze: the watchdog
restarts it and says so, at most once every two hours.
"""

import json
import msvcrt
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BOT_NAME = "Iris"
BASE = Path(__file__).resolve().parent
STATUS_FILE = BASE / "status.json"
STOP_FLAG = BASE / "stop.flag"
BOT = BASE / "iris_bot.py"
BOT_LOCK = BASE / "iris.lock"
LOG_FILE = BASE / "watchdog.log"
NO_WINDOW = 0x08000000
DETACHED = 0x00000008
NEW_GROUP = 0x00000200
BOOT_SETTLE = 15            # seconds to wait after logon before the first check
CHECK_SECONDS = 60
FROZEN_AFTER = 10 * 60      # status.json this old while the bot's lock is still held...
FROZEN_CHECKS = 3           # ...on this many checks in a row (a PC that just woke gets a pass)
FROZEN_COOLDOWN = 2 * 3600  # never restart a frozen bot more often than this


def wlog(text: str) -> None:
    """One plain line in watchdog.log. The file is kept under about 200 KB."""
    try:
        t = datetime.now()
        stamp = f"{t:%a %b} {t.day}, {t.strftime('%I').lstrip('0')}:{t:%M:%S %p}"
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 200_000:
            LOG_FILE.write_text(LOG_FILE.read_text(encoding="utf-8", errors="replace")[-100_000:],
                                encoding="utf-8")
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp}  {text}\n")
    except OSError:
        pass


def read_env() -> dict:
    """Shared\\.env (setup writes it) with the bot's own .env on top."""
    env = {}
    for path in (BASE.parent / "Shared" / ".env", BASE / ".env"):
        try:
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                key, sep, value = line.partition("=")
                if sep and not key.strip().startswith("#") and value.strip():
                    env[key.strip()] = value.strip()
        except OSError:
            pass
    return env


def notify(text: str) -> None:
    wlog(f"texting the owner: {text}")
    env = read_env()
    if env.get("MATRIX_TOKEN"):
        # iMessage via Shared\notify.py: Telegram never texts first, and
        # notify.py applies the quiet-hours rule.
        try:
            subprocess.run(
                [sys.executable, str(BASE.parent / "Shared" / "notify.py"), BOT_NAME, text],
                cwd=BASE.parent / "Shared", creationflags=NO_WINDOW, timeout=90,
                capture_output=True,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    ids = [x.strip() for x in env.get("ALLOWED_CHAT_IDS", "").split(",") if x.strip()]
    if not token or "PASTE" in token.upper() or not ids:
        return
    for cid in ids:
        try:
            data = urllib.parse.urlencode({"chat_id": cid, "text": text}).encode()
            urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=15
            )
        except OSError:
            pass


def bot_alive() -> bool:
    """True while the bot's process holds its single-instance lock."""
    try:
        fh = open(BOT_LOCK, "r+b")
    except OSError:
        return False  # no lock file yet: the bot has never run here
    try:
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return True  # held, and only the bot ever holds it
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        return False
    finally:
        fh.close()


def read_pid():
    # A read can land while the bot is swapping the file: try a few times.
    for _ in range(5):
        try:
            return json.loads(STATUS_FILE.read_text(encoding="utf-8")).get("pid")
        except (OSError, ValueError, AttributeError):
            time.sleep(1)
    return None


def status_age() -> float:
    """Seconds since the bot last rewrote status.json (0 if there's no file)."""
    try:
        return max(0.0, time.time() - STATUS_FILE.stat().st_mtime)
    except OSError:
        return 0.0


def pid_is_bot(pid) -> bool:
    """Only used before killing a frozen bot: is this pid really it? Anything
    short of a clear yes is a no."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
            capture_output=True, text=True, creationflags=NO_WINDOW, timeout=60,
        ).stdout or ""
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        return False
    return BOT.name in out


def start_bot() -> None:
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    subprocess.Popen(
        [str(exe), str(BOT)], cwd=BASE,
        creationflags=DETACHED | NEW_GROUP | NO_WINDOW,
    )


def unfreeze() -> None:
    """The bot holds its lock but hasn't touched status.json in a long while."""
    pid, mins = read_pid(), int(status_age() // 60)
    if not pid_is_bot(pid):
        wlog(f"bot looks frozen ({mins} min with no status update) but pid {pid} isn't provably it: left alone")
        notify(f"{BOT_NAME} looks frozen. Nothing from it in {mins} minutes. "
               "Open the Control Center to check on it.")
        return
    wlog(f"bot is frozen ({mins} min with no status update): restarting pid {pid}")
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))],
                       capture_output=True, creationflags=NO_WINDOW, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass
    time.sleep(5)
    if bot_alive():
        wlog("the frozen bot wouldn't die")
        notify(f"{BOT_NAME} is frozen and I couldn't restart it. Open the Control Center to check on it.")
        return
    start_bot()
    time.sleep(25)
    if bot_alive():
        wlog("bot is back after the freeze")
        notify(f"{BOT_NAME} froze, so the watchdog restarted it.")
    else:
        wlog("bot did NOT come back after the freeze")
        notify(f"{BOT_NAME} froze and didn't come back after a restart. "
               "Open the Control Center to check on it.")


def main() -> None:
    # Nothing to run yet = setup mode: a watchdog would just relaunch a bot
    # that exits at boot, forever. A bot runs on iMessage (MATRIX_TOKEN), on
    # Telegram, or both; this used to look for the Telegram token only, so
    # taking that token out would have left the bot down after the next reboot.
    env = read_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not env.get("MATRIX_TOKEN") and (not token or "PASTE" in token.upper()):
        return

    try:
        lock = open(BASE / "watchdog.lock", "w")
        lock.write("lock")
        lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return  # another watchdog is already on duty

    wlog("watchdog on duty")
    seen_alive = False  # boot-time start is silent; only revivals get announced
    failures = 0
    stale = 0
    last_unfreeze = 0.0
    time.sleep(BOOT_SETTLE)  # let the boot settle
    while True:
        try:
            if STOP_FLAG.exists():
                if seen_alive:
                    wlog("stop.flag is set: standing down")
                seen_alive = False  # intentional stop - stay down, stay quiet
                stale = 0
            elif bot_alive():
                if not seen_alive:
                    wlog(f"bot is up (pid {read_pid()})")
                seen_alive = True
                failures = 0
                stale = stale + 1 if status_age() > FROZEN_AFTER else 0
                if stale >= FROZEN_CHECKS and time.time() - last_unfreeze > FROZEN_COOLDOWN:
                    last_unfreeze, stale = time.time(), 0
                    unfreeze()
            else:
                stale = 0
                wlog("bot is not running: starting it" + (" (it was up a minute ago)" if seen_alive else ""))
                start_bot()
                time.sleep(25)
                if bot_alive():
                    wlog(f"bot is up (pid {read_pid()})")
                    if seen_alive:
                        notify(f"{BOT_NAME} went down, so the watchdog revived it.")
                    seen_alive = True
                    failures = 0
                else:
                    failures += 1
                    wlog(f"the bot didn't stay up (try {failures})")
                    if failures == 3:
                        notify(
                            f"{BOT_NAME} keeps dying and won't restart. Check the "
                            "PC when you can."
                        )
                    time.sleep(min(600, 60 * failures))
        except Exception as e:  # noqa: BLE001 - the watchdog itself must never die
            wlog(f"check failed: {type(e).__name__}: {e}")
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    main()
