"""Login for every claude call the crew makes.

Each bot scrubs ANTHROPIC_* and CLAUDE_* out of the environment it hands
claude, so a session leaked in from a parent Claude Code shell can't take
over. That scrub would also drop a setup token, so the token lives in
Shared\\.env instead and gets put back after the scrub.

The file is read fresh on every call: paste a new token in and the next turn
uses it, no restart. No token in the file means claude falls back to the
stored `claude auth login` session, same as before.

Setup makes a token with `claude setup-token` (lasts about a year, runs on
the owner's Claude plan, not API billing) and writes it into Shared\\.env as
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
"""

import json
import re
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / ".env"
KEY = "CLAUDE_CODE_OAUTH_TOKEN"


# ------------------------------------------------ when a turn fails ----
# A failed `claude -p --output-format json` still prints its JSON result. The
# message is the "result" field; the tail of the output is telemetry. Reading
# the raw output goes wrong three ways:
# - "was the session lost?" would match the word "session" anywhere, and
#   every result has a "session_id" key, so any failure would wipe the day's
#   session without archiving it;
# - the CLI says "Failed to authenticate" for a dead login, not only
#   "Not logged in", so matching one phrase misses it;
# - the owner would get texted 800 characters of JSON.

def failure_text(out: str, err: str) -> str:
    """What claude said went wrong, in its own words."""
    try:
        data = read_turns(out)
        if str(data.get("result") or "").strip():
            return str(data["result"]).strip()[:800]
    except ValueError:
        pass
    return (err or out or "").strip()[-800:]


# ------------------------------------------- reading a turn's output ----
# The bots ran `claude -p --output-format json`, which only prints the LAST
# turn's result. When a Bash call runs past 2 minutes, Claude Code moves it to
# the background, the turn ends, and when the job finishes the same process
# runs a second turn for the notification. The json result is then only that
# second turn, so a real reply followed by a follow-up turn of just
# "[REACT: 👍]" reached the owner as a bare thumbs-up. stream-json prints a
# result event per turn, so the bots read that and keep every turn's text.

_TAG = re.compile(r"\[[A-Z_]+:[^\]]*\]")
_SILENT = re.compile(r"^\W*SILENT\W*$")


def merge_replies(parts: list[str]) -> str:
    """Join the replies of every turn one claude call ran. A follow-up turn
    that says SILENT, or only repeats tags already sent, adds nothing; a
    SILENT before a real follow-up reply gets dropped."""
    kept: list[str] = []
    seen_tags: set[str] = set()
    for part in (p.strip() for p in parts):
        if not part:
            continue
        if _SILENT.match(part):
            if not kept:
                kept.append(part)
            continue
        tags = _TAG.findall(part)
        if kept and not _TAG.sub("", part).strip() and all(t in seen_tags for t in tags):
            continue
        kept = [k for k in kept if not _SILENT.match(k)]
        seen_tags.update(tags)
        kept.append(part)
    return "\n\n".join(kept)


def read_turns(out: str) -> dict:
    """Parse `claude -p --output-format stream-json --verbose` into the dict
    the old json format gave: the last result event, with "result" holding
    every turn's reply, "usage" summed over the turns and "is_error" set if
    any turn failed. Plain json output still parses. Raises ValueError when
    there's no result at all."""
    results = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            results.append(ev)
    if not results:
        data = json.loads(out)  # old single-object format; raises ValueError
        if not isinstance(data, dict):
            raise ValueError("claude output is not an object")
        return data
    data = dict(results[-1])
    usage: dict = {}
    for ev in results:
        for k, v in (ev.get("usage") or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                usage[k] = usage.get(k, 0) + v
    data["usage"] = usage
    data["is_error"] = any(ev.get("is_error") for ev in results)
    data["result"] = merge_replies([str(ev.get("result") or "") for ev in results])
    return data


_GONE = re.compile(
    r"no conversation found"
    r"|(session|conversation)\b.{0,80}(not found|does(n't| not) exist|no longer exists|was deleted)"
    r"|could(n't| not) (find|load|resume) .{0,40}(session|conversation)", re.I)
_AUTH = re.compile(
    r"not logged in|failed to authenticate|authentication[_ ]error|invalid api key"
    r"|oauth token|please run /login|claude auth login|setup-token", re.I)


def session_gone(text: str) -> bool:
    """True only when claude says the session to resume doesn't exist. A usage
    cap, a network blip or a dead login must never count."""
    return bool(_GONE.search(text or ""))


def auth_dead(text: str) -> bool:
    """True when the failure is the crew's Claude login, so the owner hears once."""
    return bool(_AUTH.search(text or ""))


def oauth_token() -> str:
    """The setup token from Shared\\.env, or "" if there isn't one."""
    try:
        text = ENV_FILE.read_text(encoding="utf-8-sig")
    except OSError:
        return ""
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == KEY:
            return value.strip().strip('"').strip("'")
    return ""


def add_token(env: dict) -> dict:
    """Put the setup token into an already-scrubbed env, if there is one,
    plus the three things every claude call the crew makes should carry:

    - CREW_BOT=1 marks the session as a bot's, so any global Claude Code
      hooks on this PC can tell a bot turn from a desktop session and stay
      out of it.
    - CLAUDE_CODE_DISABLE_AUTO_MEMORY=1: a desktop session's memory folder
      can be set in the global settings, which the bots load too. Each bot
      keeps its own Memories\\ folder and must never read or write that one.
    - PYTHONIOENCODING=utf-8: the helper scripts the bots run print emoji
      (reminder titles), and a piped Windows console is cp1252, so icloud.py
      crashed after the reminder was already made and got run twice.
    """
    token = oauth_token()
    if token:
        env[KEY] = token
    env["CREW_BOT"] = "1"
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env
