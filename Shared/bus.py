r"""File bus the bot crew talks over.

One JSONL inbox per bot at Shared\Bus\<Bot>.jsonl, one JSON object per line:
{"ts": "2026-08-07T23:58:00", "from": "Atlas", "text": "..."}. Appends are
atomic at these sizes; each bot tracks its own read cursor so restarts never
replay or drop messages.

Every message is also appended to Shared\Bus\Bus Log.md, a plain readable
record of who said what to whom (for the owner and desktop Claude; the
bots don't need it).

Import from a bot folder:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Shared"))
    from bus import send, read_new
"""

import json
from datetime import datetime
from pathlib import Path

BUS = Path(__file__).resolve().parent / "Bus"
LOG = BUS / "Bus Log.md"


def _log(now: datetime, frm: str, to: str, text: str) -> None:
    day = f"{now:%A %b} {now.day}, {now.year}"
    clock = now.strftime("%I:%M %p").lstrip("0")
    try:
        last_day = ""
        if LOG.exists():
            for line in reversed(LOG.read_text(encoding="utf-8").splitlines()):
                if line.startswith("## "):
                    last_day = line[3:]
                    break
        with LOG.open("a", encoding="utf-8") as fh:
            if not LOG.stat().st_size:
                fh.write("# Bus Log\n\nEvery crew message, oldest first.\n")
            if last_day != day:
                fh.write(f"\n## {day}\n\n")
            fh.write(f"**{clock} - {frm} to {to}:** {text}\n\n")
    except OSError:
        pass  # the log is a convenience; delivery already happened


def send(frm: str, to: str, text: str) -> None:
    BUS.mkdir(exist_ok=True)
    now = datetime.now()
    line = json.dumps({
        "ts": now.isoformat(timespec="seconds"),
        "from": frm,
        "text": text,
    })
    with (BUS / f"{to}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    _log(now, frm, to, text)


def read_new(bot: str) -> list[dict]:
    """Messages for bot since the last call. Cursor survives restarts."""
    inbox = BUS / f"{bot}.jsonl"
    cursor = BUS / f"{bot}.cursor"
    try:
        pos = int(cursor.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pos = 0
    try:
        size = inbox.stat().st_size
    except OSError:
        return []
    if size < pos:
        pos = 0  # inbox was cleared
    if size == pos:
        return []
    with inbox.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(pos)
        data = fh.read()
        pos = fh.tell()
    try:
        cursor.write_text(str(pos), encoding="utf-8")
    except OSError:
        pass
    out = []
    for ln in data.splitlines():
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return out


if __name__ == "__main__":  # tiny CLI: python bus.py send Atlas Iris "text"
    import sys
    import time
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) >= 5 and sys.argv[1] == "send":
        send(sys.argv[2], sys.argv[3], " ".join(sys.argv[4:]))
        print("sent")
    elif len(sys.argv) >= 3 and sys.argv[1] == "read":
        for m in read_new(sys.argv[2]):
            print(f"[{m['ts']}] {m['from']}: {m['text']}")
    elif len(sys.argv) >= 4 and sys.argv[1] == "ask":
        # Desktop Claude's side: send, then wait up to 5 minutes for the reply.
        send("Desktop", sys.argv[2], " ".join(sys.argv[3:]))
        print(f"sent to {sys.argv[2]}, waiting", flush=True)
        for _ in range(60):
            time.sleep(5)
            got = read_new("Desktop")
            if got:
                for m in got:
                    print(f"[{m['ts']}] {m['from']}: {m['text']}")
                break
        else:
            print("no reply yet, it lands in Bus\\Desktop.jsonl when it comes")
    else:
        print("usage: bus.py send <From> <To> <text...> | bus.py read <Bot> | bus.py ask <To> <text...>")
