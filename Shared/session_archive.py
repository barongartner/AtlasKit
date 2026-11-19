r"""Appends each finished Claude session to the bot's one readable archive.

Each bot resets to a fresh Claude session every day. Before the old one is
dropped, archive_session() reads its transcript (Claude's .jsonl under
~\.claude\projects) and appends it as one section to the bot's single
Memories\Archive.md, which grows forever, oldest at the top. The archive is
never loaded as context; the owner, desktop Claude and the bots search it
when they need to look something up.

Kept: what the owner said, what the bot said back, crew bus messages, and any
scheduled turn that actually produced a message. Dropped: tool calls, tool
output, and turns that ended in SILENT.

Usage from a bot:
    from session_archive import archive_session
    archive_session(session_id, "Atlas", BASE / "Memories" / "Archive.md")
By hand (archives without resetting anything):
    python session_archive.py <session_id> <BotName> <path to Archive.md>
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import owner  # noqa: E402

PROJECTS = Path.home() / ".claude" / "projects"
STAMP = re.compile(r"^\[([^,\]]+)(?:, ([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}))?[^\]]*\]\s*")
OTHER_PEOPLE = "(someone else's chat)"  # a turn kind that never gets written out
_PLUMBING = ("<task-notification>", "<system-reminder>", "<local-command", "<command-name>")
_FOLD = " sent more before seeing your reply"  # after the owner's name
OWNER = "owner"  # the turn kind for something the owner said


def find_transcript(session_id: str) -> Path | None:
    hits = list(PROJECTS.glob(f"*/{session_id}.jsonl"))
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _day(dt: datetime) -> str:
    return f"{dt.strftime('%A %b')} {dt.day}, {dt.year}"


def _local(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)


def _prompt_text(content) -> str | None:
    """The text of a real prompt; None for tool results and other plumbing."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts) if parts else None
    return None


def read_turns(transcript: Path) -> list[dict]:
    """One dict per prompt: when, kind, text, reply (the last thing the bot said)."""
    turns: list[dict] = []
    for line in transcript.open(encoding="utf-8", errors="replace"):
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = ev.get("message") or {}
        if ev.get("type") == "user" and not ev.get("isMeta"):
            text = _prompt_text(msg.get("content"))
            if text is None or text.startswith("Base directory for this skill"):
                continue
            if text.lstrip().startswith(_PLUMBING):
                continue  # Claude's own notices, not something anyone said
            when = _local(ev["timestamp"]) if ev.get("timestamp") else datetime.now()
            kind = OWNER
            m = STAMP.match(text)
            if m:
                label = m.group(1).strip()
                kind = OWNER if label.lower() in ("telegram", "imessage") else label
                if label.lower().startswith("imessage from"):
                    # Someone else's chat with the bot (Iris). Those live in
                    # Memories\People on this PC only and the archive can be
                    # backed up off the PC, so they stay out of it.
                    kind = OTHER_PEOPLE
                if m.group(2):
                    when = datetime.strptime(m.group(2), "%Y-%m-%d %H:%M")
                text = text[m.end():]
            if kind == OWNER and _FOLD in text.split(":", 1)[0]:
                # They sent more while the bot was still answering: the draft
                # from the turn before was thrown away, and this prompt repeats
                # what they said plus the new part. One entry, their words only.
                cut = re.split(r":\r?\n", text, maxsplit=1)  # prompts reach claude with CRLF line ends
                text = cut[1] if len(cut) == 2 else text
                if turns and turns[-1]["kind"] == OWNER:
                    when = turns.pop()["when"]
            turns.append({"when": when, "kind": kind, "text": text.strip(), "reply": ""})
        elif ev.get("type") == "assistant" and turns:
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip():
                    turns[-1]["reply"] = block["text"].strip()
    return turns


def _bus_lines(text: str) -> list[str]:
    """Pull the crew messages out of a [Bus] prompt."""
    out = []
    for line in text.splitlines():
        m = re.match(r"From (\w+) at [0-9T:-]+: (.*)", line)
        if m:
            out.append(f"**{m.group(1)}:** {m.group(2)}")
    return out


def render(turns: list[dict], bot: str, session_id: str) -> str:
    first, last = turns[0]["when"], turns[-1]["when"]
    span = _day(first) if first.date() == last.date() else f"{_day(first)} to {_day(last)}"
    lines = [f"## {span}", "", f"*Conversation {session_id[:8]}*", ""]
    head = len(lines)
    day = None
    for t in turns:
        if t["kind"] == OTHER_PEOPLE:
            continue
        last = (t["reply"].strip().splitlines() or [""])[-1]
        # Same test the bots use: a scheduled turn is silent when its last line
        # ends in SILENT, even after some narration. A reply to the owner is only
        # dropped when it is the bare word.
        silent = (t["reply"].upper().rstrip(".") == "SILENT" if t["kind"] == OWNER
                  else bool(re.search(r"\bSILENT\W*$", last)))
        if t["kind"] != OWNER and silent and t["kind"] != "Bus":
            continue  # a scheduled turn that sent nothing
        if t["when"].date() != day:
            day = t["when"].date()
            lines += [f"### {_day(t['when'])}", ""]
        stamp = _clock(t["when"])
        if t["kind"] == OWNER:
            lines += [f"#### {stamp} - {owner.NAME}", "", t["text"] or "(no text)", ""]
        elif t["kind"] == "Bus":
            lines += [f"#### {stamp} - Crew bus", ""] + _bus_lines(t["text"]) + [""]
        else:
            lines += [f"#### {stamp} - {bot} ({t['kind'].lower()})", "", t["reply"], ""]
            continue
        if t["reply"] and not silent:
            lines += [f"#### {stamp} - {bot}", "", t["reply"], ""]
    if len(lines) == head:
        return ""  # nothing was said to anyone: no empty section
    return "\n".join(lines).rstrip() + "\n"


def archive_session(session_id: str, bot: str, archive_file: Path) -> Path | None:
    """Append the session to the bot's one archive file. Returns the file, or
    None if there was nothing to archive. A session already in the file is
    never added twice."""
    transcript = find_transcript(session_id)
    if not transcript:
        return None
    turns = read_turns(transcript)
    if not turns:
        return None
    section = render(turns, bot, session_id)
    if not section:
        return None
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    if archive_file.exists():
        if f"*Conversation {session_id[:8]}*" in archive_file.read_text(encoding="utf-8", errors="replace"):
            return archive_file
    else:
        archive_file.write_text(
            f"# {bot} Archive\n\nEvery past conversation, oldest first. Not loaded as context: "
            "search it when you need something from the past.\n\n", encoding="utf-8")
    with archive_file.open("a", encoding="utf-8") as fh:
        fh.write("\n" + section)
    return archive_file


# ------------------------------------------------- daily fresh sessions ----
# A bot's day starts at 2am, when the owner is most likely asleep, so a
# late-night chat stays one conversation and the next morning is a clean slate.
DAY_STARTS_HOUR = 2


def _bot_day(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return (now.date() if now.hour >= DAY_STARTS_HOUR
            else datetime.fromordinal(now.toordinal() - 1).date()).isoformat()


def day_start(now: datetime | None = None) -> datetime:
    """When the current bot day began (today or yesterday at DAY_STARTS_HOUR)."""
    return datetime.fromisoformat(_bot_day(now)).replace(hour=DAY_STARTS_HOUR)


def current_session(session_file: Path, bot: str, archive_file: Path, log=None) -> str:
    """The session id to resume, or "" for a fresh one. When the stored
    session belongs to an earlier day, it gets archived and dropped."""
    day_file = session_file.with_name("session_day.txt")
    sid = session_file.read_text(encoding="utf-8").strip() if session_file.exists() else ""
    if not sid:
        return ""
    day = day_file.read_text(encoding="utf-8").strip() if day_file.exists() else ""
    if day == _bot_day():
        return sid
    end_session(session_file, bot, archive_file, log)
    return ""


def end_session(session_file: Path, bot: str, archive_file: Path, log=None) -> None:
    """Archive the current session (if any) and clear it so the next turn
    starts fresh. Used by the daily rollover and by /new."""
    sid = session_file.read_text(encoding="utf-8").strip() if session_file.exists() else ""
    if sid:
        try:
            path = archive_session(sid, bot, archive_file)
            if log:
                log.info("Saved the last conversation (%s) to %s", sid[:8], path)
        except Exception:  # never let archiving block the bot
            if log:
                log.exception("archiving session %s failed", sid[:8])
    session_file.unlink(missing_ok=True)
    session_file.with_name("session_day.txt").unlink(missing_ok=True)


def save_session(session_file: Path, sid: str) -> None:
    """Store the id Claude returned; a new id starts a new bot day."""
    old = session_file.read_text(encoding="utf-8").strip() if session_file.exists() else ""
    session_file.write_text(sid, encoding="utf-8")
    if sid != old:
        session_file.with_name("session_day.txt").write_text(_bot_day(), encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: python session_archive.py <session_id> <BotName> <path to Archive.md>")
    print(archive_session(sys.argv[1], sys.argv[2], Path(sys.argv[3])) or "nothing to archive")
