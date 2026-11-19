r"""iMessage for the crew, through the bridge in WSL.

The bridge (~/imessage-bridge in the WSL distro) turns every iMessage chat
into a Matrix room on the local Synapse server (http://127.0.0.1:8008). Every
bot logs in as the same Matrix user (the bridge's owner, on the server named
MATRIX_SERVER_NAME): reading a room's messages = reading the iMessage chat,
posting into the room = sending an iMessage from the bot account.

One thread per bot:
- The bot Apple ID has an alias address per bot (+atlas, +iris). All of the
  owner's threads with the account share ONE Matrix room (keyed by their
  number); the patched bridge (via_alias.go) tells them apart:
- Incoming: each message carries "com.atlas.via", the bot address the owner
  texted. A bot answers only messages whose via is its own IMESSAGE_VIA.
  The default bot (Atlas, IMESSAGE_DEFAULT=1) also answers messages to the
  addresses in IMESSAGE_ALSO (the account's main address) and untagged ones.
- Missed FaceTime call notices carry the via too, so each bot hears about
  the calls to its own address. The bots never pick up.
- Outgoing: every message a bot posts carries "com.atlas.send_from" = its
  IMESSAGE_VIA, so the bridge sends it from that address and it lands in
  that bot's own thread on the owner's iPhone.
- Only senders listed in IMESSAGE_OWNER are treated as the owner. Anyone
  else's texts are logged and left alone, except by a bot with
  IMESSAGE_ANSWER_OTHERS (Iris), which gets them flagged as not the owner.

IMessageBot mimics the slice of python-telegram-bot's Bot the crew code uses
(send_message, send_photo, send_document, send_video, send_audio,
send_voice, send_chat_action), so replies, file tags and typing dots go
through the same functions for both apps.

Settings: each key is read from the bot's own .env first, then from
Shared\\.env (setup writes them there), then falls back to the owner config.
MATRIX_TOKEN (required to turn iMessage on), MATRIX_URL (default
http://127.0.0.1:8008), IMESSAGE_OWNER (comma list, e.g. tel:+15551234567;
default: OWNER_PHONE plus OWNER_IMESSAGE), IMESSAGE_HOME_ROOM (room for messages the bot starts
itself), IMESSAGE_MGMT_ROOM (the bridge's admin room, for opening new chats),
IMESSAGE_VIA (this bot's own address, e.g. mailto:bots+atlas@example.com;
default the +bot alias of BOT_APPLE_ID), IMESSAGE_DEFAULT (1 for Atlas, the
default), IMESSAGE_ALSO (Atlas only: other addresses it answers for; default
BOT_APPLE_ID itself), IMESSAGE_ANSWER_OTHERS (1 for Iris, the default).
"""

import asyncio
import json
import logging
import mimetypes
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import owner  # noqa: E402

log = logging.getLogger("imessage")
BRIDGE_BOT_PREFIX = "@imessagebot:"
GHOST_PREFIX = "@imessage_"
VIA_KEY = "com.atlas.via"              # set by the bridge on incoming messages
# The bridge's m.bridge state key: "<Matrix server name>/imessage".
BRIDGE_STATE_KEY = f"{owner.MATRIX_SERVER}/imessage"
SEND_FROM_KEY = "com.atlas.send_from"  # set by bots on outgoing messages
# A full http(s) link. split_links() trims what belongs to the sentence around it.
LINK_RE = re.compile(r"https?://[^\s<>]+")
_LINK_TAIL = ".,;:!?'\"’”"  # sentence punctuation that can trail a link


def split_links(text: str) -> list[str]:
    """`text` as the bubbles to send: each link on its own, the way an iPhone
    splits them, so the bridge sends a rich preview instead of a raw URL
    mid-text. A bracket stays on the link when the link opened it
    (.../Atlas_(mythology)); the old pattern cut every link at its first
    bracket. Pieces with nothing to read (a stray bracket or period) are
    dropped, but an emoji on its own is kept."""
    parts, last = [], 0
    for m in LINK_RE.finditer(text):
        url = m.group(0)
        while url and (url[-1] in _LINK_TAIL
                       or (url[-1] == ")" and url.count("(") < url.count(")"))
                       or (url[-1] == "]" and url.count("[") < url.count("]"))):
            url = url[:-1]
        if len(url) < len("https://x"):
            continue
        parts += [text[last:m.start()], url]
        last = m.start() + len(url)
    parts.append(text[last:])
    kept = [p.strip() for p in parts if re.search(r"\w|[^\x00-\x7f]", p or "")]
    return kept or [text]


# ------------------------------------------------------- effects & style ----
# What a person can do from the iPhone keyboard, so the bots text like a
# person would. The bridge turns these
# into the real thing (outbound_style.go and parse_html_to_parts in the WSL
# bridge):
#   [EFFECT: slam]  bubble effects slam, loud, gentle, invisible ink, and the
#                   screen effects echo, spotlight, balloons, confetti, love,
#                   lasers, fireworks, celebration, shooting star
#   **bold**  *italic*  __underline__  ~~strikethrough~~
#   [BIG: words]  animated text: big, small, shake, nod, explode, ripple,
#                 bloom, jitter

URGENT_RE = re.compile(r"\[URGENT\]\s*", re.I)    # Notify Anyway: break through their Focus
EDIT_RE = re.compile(r"^\s*\[EDIT\]\s*", re.I)      # correct the last text (Apple: 15 minutes)
UNSEND_RE = re.compile(r"^\s*\[UNSEND\]\s*", re.I)  # take back the last text (Apple: 2 minutes)
EFFECT_RE = re.compile(r"\[EFFECT:\s*([^\]]+?)\s*\]\s*", re.I)
ANIMATIONS = ("big", "small", "shake", "nod", "explode", "ripple", "bloom", "jitter")
ANIMATE_RE = re.compile(r"\[(" + "|".join(ANIMATIONS) + r"):\s*([^\]]+?)\s*\]", re.I)
STYLE_RES = [  # (pattern, tag); the markers have to hug the words like a person types them
    (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"), "b"),
    (re.compile(r"~~(?=\S)(.+?)(?<=\S)~~"), "s"),
    (re.compile(r"(?<![\w_])__(?=\S)(.+?)(?<=\S)__(?![\w_])"), "u"),
    (re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])"), "i"),
]


def take_effect(text: str) -> tuple[str, str]:
    """Pull an [EFFECT: ...] tag out of a reply: (effect name, rest)."""
    m = EFFECT_RE.search(text)
    if not m:
        return "", text
    return m.group(1).strip().lower(), (text[:m.start()] + text[m.end():]).strip()


def _esc(s: str) -> str:
    # Only what the bridge's HTML parser decodes.
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def style_text(text: str) -> tuple[str, str]:
    """(plain text, HTML or "") for one bubble. Links are left alone so a
    URL's underscores and asterisks never turn into formatting."""
    if not (ANIMATE_RE.search(text) or any(r.search(text) for r, _ in STYLE_RES)):
        return text, ""
    if LINK_RE.search(text):
        return text, ""
    spans: list[tuple[int, int, str, str, str]] = []  # start, end, inner, open, close
    for m in ANIMATE_RE.finditer(text):
        spans.append((m.start(), m.end(), m.group(2),
                      f'<span data-mx-imessage-effect="{m.group(1).lower()}">', "</span>"))
    taken = [(a, b) for a, b, *_ in spans]
    for rx, tag in STYLE_RES:
        for m in rx.finditer(text):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            spans.append((m.start(), m.end(), m.group(1), f"<{tag}>", f"</{tag}>"))
            taken.append((m.start(), m.end()))
    if not spans:
        return text, ""
    spans.sort()
    plain, html, last = [], [], 0
    for a, b, inner, op, cl in spans:
        plain += [text[last:a], inner]
        html += [_esc(text[last:a]), op, _esc(inner), cl]
        last = b
    plain.append(text[last:])
    html.append(_esc(text[last:]))
    return "".join(plain), "".join(html)


async def _to_ogg_opus(src: Path):
    """`src` as a temp .ogg Opus voice note (mono, 48 kHz, like an iPhone's),
    or None if ffmpeg can't read it. The caller deletes the file."""
    import tempfile
    fd, out = tempfile.mkstemp(prefix="voice-", suffix=".ogg")
    os.close(fd)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "48000",
        "-c:a", "libopus", "-b:a", "32k", out,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0 or os.path.getsize(out) == 0:
        log.warning("voice note conversion of %s failed: %s", src.name, (err or b"").decode(errors="replace")[-300:])
        os.unlink(out)
        return None
    return Path(out)


class IMessageError(OSError):
    """Raised on a failed send. An OSError so the crew's existing
    `except (TelegramError, OSError)` fallbacks already catch it. `status` is
    the HTTP status Synapse answered with, 0 when it never answered
    (connection refused, timeout, dropped socket), and -1 for everything
    else (no such chat, not a file, a person who isn't on iMessage)."""

    def __init__(self, message: str, status: int = -1):
        super().__init__(message)
        self.status = status


@dataclass
class Incoming:
    room: str
    event_id: str
    sender: str          # iMessage handle, e.g. tel:+15551234567
    kind: str            # text, image, video, audio, file, unreadable, missed_call
    body: str = ""
    mxc: str = ""
    filename: str = ""
    mimetype: str = ""
    voice: bool = False
    edited: bool = False
    via: str = ""        # which of the account's addresses the owner texted
    from_owner: bool = True  # False = someone else (only bots with IMESSAGE_ANSWER_OTHERS)
    is_group: bool = False   # a group chat (only bots with IMESSAGE_ANSWER_OTHERS see these)
    reply_to: str = ""       # event id this message replies to, if any
    raw: dict = field(default_factory=dict)


def decode_ghost(user_id: str) -> str:
    """@imessage_tel=3a+15551234567:<server> -> tel:+15551234567"""
    local = user_id.split(":", 1)[0][len(GHOST_PREFIX):]
    out, i = [], 0
    while i < len(local):
        ch = local[i]
        if ch == "=" and i + 3 <= len(local) and re.fullmatch(r"[0-9a-f]{2}", local[i + 1:i + 3]):
            out.append(chr(int(local[i + 1:i + 3], 16)))
            i += 3
        elif ch == "_" and i + 1 < len(local):
            out.append(local[i + 1].upper())
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def normalize_handle(raw: str) -> str:
    """Any phone/email spelling -> the bridge's form: tel:+15551234567 or
    mailto:name@x.com. "" if it doesn't look like either."""
    r = (raw or "").strip()
    if "@" in r:
        return "mailto:" + r.split(":", 1)[-1].lower() if r.lower().startswith("mailto:") else "mailto:" + r.lower()
    digits = re.sub(r"\D", "", r)
    if len(digits) == 10:
        digits = "1" + digits  # North American number without the country code
    return f"tel:+{digits}" if len(digits) >= 8 else ""


def split_portal_id(portal_id: str) -> tuple[str, str]:
    """"tel:+1555...|via=mailto:x" -> ("tel:+1555...", "mailto:x")."""
    peer, _, via = portal_id.partition("|via=")
    return peer, via


def _setting(env: dict, key: str, default: str = "") -> str:
    """A setting from the bot's own env, else Shared\\.env, else `default`."""
    return (env.get(key) or "").strip() or owner.get(key) or default


def _flag(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes")


def _mailto(address: str) -> str:
    """An email address in the bridge's form ("" stays "")."""
    a = (address or "").strip().lower()
    return a if not a or a.startswith("mailto:") else "mailto:" + a


class Matrix:
    def __init__(self, url: str, token: str, state_file: Path):
        self.url = url.rstrip("/")
        self.token = token
        self.state_file = state_file
        self.user_id = ""
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=90.0),
                                      headers={"Authorization": f"Bearer {token}"})
        self._portal_cache: dict[str, str] = {}
        self._upload_limit: int | None = None
        # Transaction IDs must be unique across EVERY sender sharing this Matrix
        # user+token (every bot does). Synapse dedups by (user, device, txn),
        # so two bots emitting the same txn means the second is silently dropped
        # as a "retry" - no error, nothing posted. A clock seed wasn't enough:
        # the bots start together and their counters drift into each other,
        # which silently eats every bot's replies after some hours. A
        # random per-process prefix gives each bot its own txn namespace.
        self._txn_prefix = secrets.token_hex(5)
        self._txn = 0

    # ------------------------------------------------------------ state ----
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self, **kv) -> None:
        st = self._load_state()
        st.update(kv)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
        tmp.replace(self.state_file)

    def state(self, key: str, default=None):
        return self._load_state().get(key, default)

    # ------------------------------------------------------------- http ----
    async def call(self, method: str, path: str, **kw) -> dict:
        try:
            r = await self.http.request(method, self.url + path, **kw)
        except httpx.HTTPError as e:
            # A dead or slow bridge raises httpx's own errors, and those are not
            # OSErrors, so every `except OSError` around a send or a read used
            # miss them: a failed mark_read or a finished reply that never
            # got sent would go unnoticed.
            raise IMessageError(f"{method} {path.split('?')[0]} -> {type(e).__name__} {e}".strip(), 0) from e
        if r.status_code >= 400:
            raise IMessageError(f"{method} {path.split('?')[0]} -> {r.status_code} {r.text[:200]}",
                                r.status_code)
        return r.json() if r.content else {}

    def _next_txn(self) -> str:
        self._txn += 1
        return f"crew-{self._txn_prefix}-{self._txn}"

    async def whoami(self) -> str:
        self.user_id = (await self.call("GET", "/_matrix/client/v3/account/whoami"))["user_id"]
        return self.user_id

    async def portal_id(self, room: str) -> str:
        """The bridge's id for a room's chat ('' for non-bridge rooms)."""
        if room not in self._portal_cache:
            pid = ""
            try:
                ev = await self.call("GET", f"/_matrix/client/v3/rooms/{quote(room)}/state/m.bridge/"
                                            + quote(BRIDGE_STATE_KEY, safe=""))
                pid = (ev.get("channel") or {}).get("id", "")
            except IMessageError as e:
                # Only "no such state" (404) or "not in that room" (403) is an
                # answer. Synapse being down or busy is not: that must reach
                # the caller, never get remembered as "not an iMessage chat"
                # (the room would be ignored until the bot restarts).
                if e.status not in (403, 404):
                    raise
                try:  # older bridges only set the half-shot type
                    for ev in await self.call("GET", f"/_matrix/client/v3/rooms/{quote(room)}/state"):
                        if ev.get("type") in ("m.bridge", "uk.half-shot.bridge"):
                            pid = (ev["content"].get("channel") or {}).get("id", "")
                            break
                except IMessageError as e2:
                    if e2.status not in (403, 404):
                        raise
                    pid = ""
                except (TypeError, KeyError):
                    pid = ""
            self._portal_cache[room] = pid
        return self._portal_cache[room]

    # ------------------------------------------------------------ sends ----
    async def send_event(self, room: str, content: dict, etype: str = "m.room.message") -> str:
        path = f"/_matrix/client/v3/rooms/{quote(room)}/send/{etype}/{self._next_txn()}"
        # Synapse not answering (restarting, WSL just woke) gets three more
        # tries over 30 seconds before the send is given up on. Every try
        # reuses the same transaction id, so Synapse can never post it twice.
        # A 429 (too many bubbles too fast: a brief with seven links is about
        # fifteen sends) waits as long as Synapse says and tries again, so a
        # long brief doesn't get cut off.
        transport_tries = 0
        for attempt in range(12):
            try:
                return (await self.call("PUT", path, json=content)).get("event_id", "")
            except IMessageError as e:
                if e.status == 429 and attempt < 11:
                    m = re.search(r'"retry_after_ms"\s*:\s*(\d+)', str(e))
                    wait = min(int(m.group(1)) / 1000 if m else 3.0, 30.0) + 0.5
                    log.warning("Synapse says slow down, sending again in %.1fs", wait)
                    await asyncio.sleep(wait)
                    continue
                transport_tries += 1
                if e.status != 0 or transport_tries == 4:
                    raise
                log.warning("send didn't get through (%s), trying again in %ss", e, 5 * transport_tries)
                await asyncio.sleep(5 * transport_tries)
        return ""

    async def upload_limit(self) -> int:
        """The biggest file Synapse takes, in bytes (0 if it won't say).
        Asked once per process."""
        if self._upload_limit is None:
            for path in ("/_matrix/client/v1/media/config", "/_matrix/media/v3/config"):
                try:
                    self._upload_limit = int((await self.call("GET", path)).get("m.upload.size") or 0)
                    break
                except (IMessageError, TypeError, ValueError):
                    continue
            else:
                return 0  # couldn't ask: try again on the next upload
        return self._upload_limit

    async def upload(self, path: Path, mimetype: str) -> str:
        # Say so up front when a file is over Synapse's cap (50 MB unless
        # max_upload_size is raised): a bigger file would otherwise die on a
        # timeout with nobody told.
        size, limit = path.stat().st_size, await self.upload_limit()
        if limit and size > limit:
            raise IMessageError(f"{path.name} is {size / 1048576:.0f} MB and the most I can send "
                                f"is {limit / 1048576:.0f} MB", 413)
        data = path.read_bytes()
        r = await self.call("POST", f"/_matrix/media/v3/upload?filename={quote(path.name)}",
                            content=data, headers={"Content-Type": mimetype},
                            timeout=httpx.Timeout(60.0, read=600.0, write=600.0))
        return r["content_uri"]

    async def download(self, mxc: str, dest: Path) -> Path:
        if not mxc.startswith("mxc://") or "/" not in mxc[len("mxc://"):]:
            raise IMessageError(f"nothing to download ({mxc or 'no attachment'})")
        server, media_id = mxc[len("mxc://"):].split("/", 1)
        for path in (f"/_matrix/client/v1/media/download/{server}/{media_id}",
                     f"/_matrix/media/v3/download/{server}/{media_id}"):
            try:
                r = await self.http.get(self.url + path)
            except httpx.HTTPError as e:
                raise IMessageError(f"couldn't download {mxc}: {type(e).__name__} {e}".strip(), 0) from e
            if r.status_code < 400:
                dest.write_bytes(r.content)
                return dest
        raise IMessageError(f"couldn't download {mxc}")

    async def redact(self, room: str, event_id: str) -> None:
        """Unsend: the bridge turns a redaction of our own message into an
        iMessage unsend (Apple allows it for 2 minutes)."""
        path = f"/_matrix/client/v3/rooms/{quote(room)}/redact/{quote(event_id)}/{self._next_txn()}"
        await self.call("PUT", path, json={})

    async def typing(self, room: str, on: bool, timeout_ms: int = 30000) -> None:
        body = {"typing": on, "timeout": timeout_ms} if on else {"typing": False}
        await self.call("PUT", f"/_matrix/client/v3/rooms/{quote(room)}/typing/{quote(self.user_id)}",
                        json=body)

    async def mark_read(self, room: str, event_id: str) -> None:
        await self.call("POST", f"/_matrix/client/v3/rooms/{quote(room)}/receipt/m.read/{quote(event_id)}",
                        json={})

    async def join(self, room: str) -> None:
        await self.call("POST", f"/_matrix/client/v3/join/{quote(room)}", json={})


class IMessage:
    """One bot's connection: who it is, which rooms it owns, and the sync loop."""

    SOURCE = "iMessage"

    # A person doesn't flip to "Read" the instant a text lands, and doesn't
    # start typing before they've read it. The read receipt goes out after a
    # short glance, like a person texting back, and typing dots wait for it.
    READ_DELAY = (1.2, 3.5)  # seconds, picked at random per message

    def seen(self, room: str, event_id: str) -> None:
        """Mark `event_id` read a moment from now, like someone glancing at
        their phone. Never blocks the caller; typing dots in `room` hold off
        until the receipt has gone out."""
        import random
        reads = self.__dict__.setdefault("_reads", {})
        done = asyncio.Event()
        reads[room] = done

        async def glance() -> None:
            try:
                await asyncio.sleep(random.uniform(*self.READ_DELAY))
                await self.mx.mark_read(room, event_id)
            except Exception:  # cosmetic; a missed receipt never fails a turn
                log.debug("read receipt for %s didn't go out", event_id, exc_info=True)
            finally:
                done.set()

        task = asyncio.get_running_loop().create_task(glance())
        self.__dict__.setdefault("_read_tasks", set()).add(task)
        task.add_done_callback(self._read_tasks.discard)

    async def read_first(self, room: str) -> None:
        """Wait (at most a few seconds) for the read receipt `seen` scheduled."""
        done = self.__dict__.get("_reads", {}).get(room)
        if done is not None and not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), timeout=self.READ_DELAY[1] + 5)
            except asyncio.TimeoutError:
                pass

    def __init__(self, bot_name: str, env: dict, state_file: Path):
        self.bot_name = bot_name
        is_atlas = bot_name.lower() == "atlas"
        self.mx = Matrix(_setting(env, "MATRIX_URL", "http://127.0.0.1:8008"),
                         _setting(env, "MATRIX_TOKEN"), state_file)
        self.owners = {normalize_handle(h) or h.strip().lower()
                       for h in _setting(env, "IMESSAGE_OWNER", owner.HANDLES).split(",") if h.strip()}
        self.via = _setting(env, "IMESSAGE_VIA", _mailto(owner.bot_address(bot_name))).lower()
        self.default = _flag(_setting(env, "IMESSAGE_DEFAULT", "1" if is_atlas else ""))
        self.also = {h.strip().lower() for h in
                     _setting(env, "IMESSAGE_ALSO", _mailto(owner.BOT_APPLE_ID) if is_atlas else "").split(",")
                     if h.strip()}
        # Iris only: messages from people other than the owner get passed on too
        # (flagged from_owner=False, so the bot can lock those turns down).
        self.answer_others = _flag(_setting(env, "IMESSAGE_ANSWER_OTHERS",
                                            "1" if bot_name.lower() == "iris" else ""))
        self.mgmt_room = _setting(env, "IMESSAGE_MGMT_ROOM")
        self._dispatched: dict[str, None] = {}  # event ids already handed to the bot (newest last)
        self.last_event: dict[str, str] = {}  # room -> newest message from the owner/others (reaction target)
        # room -> the last 30 messages in, as (event id, text), so a tapback or
        # a swipe-reply can land on an older one the way a person scrolls up.
        self.recent_in: dict[str, list[tuple[str, str]]] = {}
        self.reply_next: dict[str, str] = {}  # room -> event the next text should be a threaded reply to
        # Recent event ids this bot sent through its own turns. Kept in the state
        # file so a restart still knows which texts in the thread were its own
        # (anything else from its address came from notify.py or a script).
        self.sent_events: list[str] = list(self.mx.state("sent_events", []))[-300:]
        self.remember_sends = True  # notify.py turns this off: its texts aren't the bot's turns
        # Own sends from before that list existed can't be told apart, so only
        # texts after this moment ever get called "outside this chat".
        self.tracking_since_ms = self.mx.state("tracking_since_ms")
        if self.tracking_since_ms is None:
            self.tracking_since_ms = int(time.time() * 1000)
            self.mx._save_state(tracking_since_ms=self.tracking_since_ms)
        self._home_env = _setting(env, "IMESSAGE_HOME_ROOM")
        self.bot = IMessageBot(self)

    @staticmethod
    def configured(env: dict) -> bool:
        return bool(_setting(env, "MATRIX_TOKEN"))

    # ---------------------------------------------------- message ownership ----
    def owns_via(self, via: str) -> bool:
        """Is a message the owner sent to this address for this bot?"""
        v = via.strip().lower()
        if v and v == self.via:
            return True
        return self.default and (not v or v in self.also)

    def home_room(self) -> str:
        """Where messages this bot starts itself go: its own thread with the owner
        once one exists, else the configured shared room."""
        return self.mx.state("home_room") or self._home_env

    # --------------------------------------- what the owner actually sees ----
    # A bot's Claude session only knows the turns it ran itself. Texts that
    # land in its thread any other way (notify.py from a long job, a desktop
    # Claude session, a watchdog) would be invisible to it, so the owner's
    # follow-ups to those would make no sense to it. These read the thread
    # itself, which is what's on the owner's phone.

    def _is_mine(self, ev: dict) -> bool:
        """Sent from this bot's own address (by anyone using its account)."""
        c = ev.get("content") or {}
        return (ev.get("sender") == self.mx.user_id
                and (c.get(SEND_FROM_KEY) or "").strip().lower() == self.via)

    def _is_his(self, ev: dict) -> bool:
        """The owner texting this bot's address."""
        sender = ev.get("sender", "")
        return (sender.startswith(GHOST_PREFIX)
                and decode_ghost(sender).lower() in self.owners
                and self.owns_via((ev.get("content") or {}).get(VIA_KEY, "")))

    @staticmethod
    def _event_text(ev: dict) -> str:
        c = ev.get("content") or {}
        if (c.get("m.relates_to") or {}).get("rel_type") == "m.replace":
            c = c.get("m.new_content") or c
        kind = c.get("msgtype", "m.text")
        body = (c.get("body") or "").strip()
        if kind in ("m.image", "m.video", "m.audio", "m.file"):
            label = {"m.image": "picture", "m.video": "video", "m.audio": "audio"}.get(kind, "file")
            return f"[{label}: {c.get('filename') or body or 'attachment'}]"
        return body

    @staticmethod
    def _when(ts_ms: int) -> str:
        dt = datetime.fromtimestamp(ts_ms / 1000)
        clock = dt.strftime("%I:%M %p").lstrip("0")
        return clock if dt.date() == datetime.now().date() else f"{dt.strftime('%a')} {clock}"

    async def thread(self, room: str, since_ms: int, limit: int = 40) -> list[dict]:
        """This bot's thread with the owner from `since_ms` on, oldest first, at
        most `limit` messages: {ts, event_id, who ("owner"/"you"), text}."""
        if not self.mx.user_id:
            await self.mx.whoami()
        found: list[dict] = []
        frm = None
        flt = json.dumps({"types": ["m.room.message"]})
        for _ in range(6):  # 6 x 100 events back is plenty for a day
            params = {"dir": "b", "limit": "100", "filter": flt}
            if frm:
                params["from"] = frm
            resp = await self.mx.call("GET", f"/_matrix/client/v3/rooms/{quote(room)}/messages", params=params)
            chunk = resp.get("chunk") or []
            for ev in chunk:
                if ev.get("origin_server_ts", 0) < since_ms:
                    chunk = []
                    break
                if ((ev.get("content") or {}).get("m.relates_to") or {}).get("rel_type") == "m.replace":
                    continue  # edits: the original is already there
                who = "you" if self._is_mine(ev) else "owner" if self._is_his(ev) else ""
                if who:
                    found.append({"ts": ev["origin_server_ts"], "event_id": ev["event_id"],
                                  "who": who, "text": self._event_text(ev)})
            if not chunk or not resp.get("end") or len(found) >= limit:
                break
            frm = resp["end"]
        return sorted(found, key=lambda m: m["ts"])[-limit:]

    async def catch_up(self, room: str) -> str:
        """A note for the top of the next prompt: texts from this bot's address
        since its last turn that no turn of its own sent (notify.py, a script,
        a desktop session). Today only: each day at DAY_STARTS_HOUR is a clean
        slate on purpose, so nothing from before it carries over.
        "" if there's nothing."""
        from session_archive import day_start  # local: notify.py needn't load it
        now_ms = int(time.time() * 1000)
        last = self.mx.state("caught_up_ms")
        self.mx._save_state(caught_up_ms=now_ms)
        if last is None:
            return ""  # first run with this code: start counting from now
        since = max(int(last), self.tracking_since_ms, int(day_start().timestamp() * 1000))
        try:
            msgs = await self.thread(room, since, limit=25)
        except Exception as e:  # never block a turn over this
            log.warning("thread catch-up failed: %s", e)
            return ""
        own = set(self.sent_events)
        outside = [m for m in msgs if m["who"] == "you" and m["event_id"] not in own]
        if not outside:
            return ""
        lines = "\n".join(
            f"- {self._when(m['ts'])}: {m['text'] if len(m['text']) <= 700 else m['text'][:700] + '...'}"
            for m in outside)
        return (f"[These went to {owner.NAME} from your number since your last turn, but not from "
                "this chat: notify.py, a script or a desktop Claude session sent them. They saw them "
                "as texts from you, so own them and expect follow-ups about them:]\n" + lines + "\n\n")

    async def quote_of(self, room: str, event_id: str) -> str:
        """'[Swipe-reply to ...]' for a message the owner replied to inline, so the
        bot knows which text they mean instead of guessing ("" if unknown)."""
        if not event_id:
            return ""
        try:
            if not self.mx.user_id:
                await self.mx.whoami()
            ev = await self.mx.call("GET", f"/_matrix/client/v3/rooms/{quote(room)}/event/{quote(event_id)}")
        except Exception as e:
            log.warning("couldn't fetch the message they replied to: %s", e)
            return ""
        text = self._event_text(ev)
        if len(text) > 700:
            text = text[:700] + "..."
        if ev.get("sender") == self.mx.user_id:
            whose = "your" if self._is_mine(ev) else "another bot's"
            if (self._is_mine(ev) and event_id not in self.sent_events
                    and ev.get("origin_server_ts", 0) >= self.tracking_since_ms):
                whose = "your (sent from your number outside this chat)"
        else:
            whose = "their own"
        return f'[Swipe-reply to {whose} text from {self._when(ev.get("origin_server_ts", 0))}: "{text}"]\n'

    async def find_room(self, handle: str) -> str:
        """The existing room for a 1:1 chat with `handle`, or "" (never opens one)."""
        target = normalize_handle(handle)
        joined = (await self.mx.call("GET", "/_matrix/client/v3/joined_rooms")).get("joined_rooms", [])
        for room in joined:
            pid = await self.mx.portal_id(room)
            if pid and normalize_handle(split_portal_id(pid)[0]) == target:
                return room
        return ""

    async def room_for(self, handle: str, wait_s: int = 60) -> str:
        """The Matrix room for a 1:1 chat with `handle`, opening a new iMessage
        chat through the bridge (start-chat) if there isn't one yet. The bot's
        own sync loop joins the bridge's invite; this waits for that."""
        target = normalize_handle(handle)
        if not target:
            raise IMessageError(f"not a phone number or email: {handle}")

        async def find() -> str:
            return await self.find_room(target)

        room = await find()
        if room:
            return room
        if not self.mgmt_room:
            raise IMessageError("IMESSAGE_MGMT_ROOM isn't set, so I can't open new chats")
        await self.mx.send_event(self.mgmt_room, {"msgtype": "m.text",
                                                  "body": "start-chat " + target.split(":", 1)[1]})
        for _ in range(wait_s // 2):
            await asyncio.sleep(2)
            room = await find()
            if room:
                return room
        raise IMessageError(f"couldn't open a chat with {target} (not on iMessage, or Apple refused the lookup)")

    # ------------------------------------------- unanswered across restarts ----
    # The sync loop hands messages off without waiting for the reply, so the
    # sync position moves on right away. A message whose turn is cut short by a
    # restart would be lost; instead it stays on this list until its turn ends
    # and is fetched and handled again when the bot comes back.
    def hold(self, room: str, event_id: str) -> None:
        held = [h for h in self.mx.state("unanswered", []) if h["event_id"] != event_id]
        held.append({"room": room, "event_id": event_id, "at": time.time()})
        self.mx._save_state(unanswered=held[-50:])

    def settle(self, event_ids: list[str]) -> None:
        if event_ids:
            done = set(event_ids)
            self.mx._save_state(unanswered=[h for h in self.mx.state("unanswered", [])
                                            if h["event_id"] not in done])

    async def replay(self, on_message) -> None:
        """Handle again whatever was still unanswered when the bot stopped
        (only the last hour: older than that, the moment has passed)."""
        held = self.mx.state("unanswered", [])
        self.mx._save_state(unanswered=[])
        for h in held:
            if time.time() - h.get("at", 0) > 3600:
                continue
            try:
                ev = await self.mx.call("GET", f"/_matrix/client/v3/rooms/{quote(h['room'])}"
                                               f"/event/{quote(h['event_id'])}")
            except IMessageError as e:
                log.warning("couldn't refetch unanswered %s: %s", h["event_id"], e)
                continue
            log.info("Picking up a message I hadn't answered before the restart")
            await self._dispatch(h["room"], ev, on_message)

    # ------------------------------------------------------------- the loop ----
    async def run(self, on_message) -> None:
        """Sync forever, calling `await on_message(Incoming)` for each message
        from the owner in a room this bot owns. Never raises; reconnects."""
        backoff = 5
        while True:
            try:
                if not self.mx.user_id:
                    await self.mx.whoami()
                    log.info("iMessage connected as %s (via=%s, default=%s)",
                             self.mx.user_id, self.via or "-", self.default)
                    await self.replay(on_message)
                since = self.mx.state("since")
                if not since:
                    # First run ever: start from now, don't replay history.
                    first = await self.mx.call("GET", "/_matrix/client/v3/sync",
                                               params={"timeout": "0", "filter": _FILTER})
                    self.mx._save_state(since=first["next_batch"])
                    continue
                resp = await self.mx.call("GET", "/_matrix/client/v3/sync",
                                          params={"since": since, "timeout": "30000", "filter": _FILTER})
                await self._handle_sync(resp, on_message)
                self.mx._save_state(since=resp["next_batch"])
                backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as e:  # bridge/Synapse down, WSL asleep, ...
                log.warning("iMessage sync failed (%s), retrying in %ss", str(e)[:200], backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    async def _handle_sync(self, resp: dict, on_message) -> None:
        rooms = resp.get("rooms") or {}
        for room in (rooms.get("invite") or {}):
            try:
                await self.mx.join(room)  # new chats arrive as invites from the bridge
                log.info("joined new iMessage chat room %s", room)
            except IMessageError as e:
                log.warning("couldn't join %s: %s", room, e)
        for room, data in (rooms.get("join") or {}).items():
            events = (data.get("timeline") or {}).get("events") or []
            pid = await self.mx.portal_id(room) if events else ""
            if not pid:
                continue  # not an iMessage chat (e.g. the bridge's admin room)
            if ("," in pid or pid.startswith("gid:")) and not self.answer_others:
                continue  # only Iris joins in on group chats
            for ev in events:
                await self._dispatch(room, ev, on_message)

    async def _dispatch(self, room: str, ev: dict, on_message) -> None:
        """One timeline event: decide whether it's this bot's to answer, then
        hand it to on_message."""
        pid = await self.mx.portal_id(room)
        is_group = "," in pid or pid.startswith("gid:")
        if is_group and not self.answer_others:
            return
        inc = self._parse(room, ev)
        if inc is None:
            return
        inc.is_group = is_group
        if inc.edited and not inc.via:
            # The bridge doesn't tag edits with the address they went to, so
            # an edit belongs to whichever bot got the original message (an
            # untagged edit of an Iris text would otherwise go to Atlas).
            inc.via = await self._via_of(room, ((ev.get("content") or {}).get("m.relates_to") or {})
                                         .get("event_id", ""))
        notice = inc.kind == "missed_call"
        if notice:  # the notice comes from the bridge bot
            # The bot whose address was called (notices with no via go to
            # the default bot, Atlas).
            if not self.owns_via(inc.via):
                return
            inc.sender = split_portal_id(pid)[0]
        elif not self.owns_via(inc.via):
            return  # the owner texted another bot's address
        if inc.sender.lower() not in self.owners:
            if not self.answer_others or notice:
                log.info("iMessage from %s (not the owner) left alone: %s", inc.sender, inc.body[:80])
                return
            inc.from_owner = False
        elif self.mx.state("home_room") != room and not is_group:
            # Where the owner talks to this bot. 1:1 chats only: a group with
            # the owner in it must never become the place private notes go.
            self.mx._save_state(home_room=room)
        # A sync that has to be run again (its position didn't get saved) or a
        # replay that overlaps the sync must never hand a message over twice.
        if inc.event_id in self._dispatched:
            return
        self._dispatched[inc.event_id] = None
        if len(self._dispatched) > 500:
            del self._dispatched[next(iter(self._dispatched))]
        if not notice:
            self.last_event[room] = inc.event_id  # what a reaction lands on
            seen = self.recent_in.setdefault(room, [])
            seen.append((inc.event_id, inc.body or ""))
            del seen[:-30]
        try:
            await on_message(inc)
        except Exception:
            log.exception("handling iMessage %s failed", inc.event_id)

    async def _via_of(self, room: str, event_id: str) -> str:
        """The bot address an earlier message was sent to ("" if unknown)."""
        if not event_id:
            return ""
        try:
            ev = await self.mx.call("GET", f"/_matrix/client/v3/rooms/{quote(room)}/event/{quote(event_id)}")
        except IMessageError:
            return ""
        return (ev.get("content") or {}).get(VIA_KEY, "")

    def _parse(self, room: str, ev: dict) -> Incoming | None:
        if ev.get("type") != "m.room.message":
            return None
        sender = ev.get("sender", "")
        c = ev.get("content") or {}
        if sender.startswith(BRIDGE_BOT_PREFIX):
            if "Missed FaceTime call" in c.get("body", ""):
                return Incoming(room, ev["event_id"], "", "missed_call", c.get("body", ""),
                                via=c.get(VIA_KEY, ""), raw=ev)
            return None
        if not sender.startswith(GHOST_PREFIX):
            return None  # our own sends (every bot posts as the same user)
        via = c.get(VIA_KEY, "")
        rel = c.get("m.relates_to") or {}
        edited = rel.get("rel_type") == "m.replace"
        if edited:
            c = c.get("m.new_content") or c
        handle = decode_ghost(sender)
        mtype = c.get("msgtype", "m.text")
        body = c.get("body", "")
        inc = Incoming(room, ev["event_id"], handle, "text", body, edited=edited, via=via, raw=ev)
        inc.reply_to = ((rel.get("m.in_reply_to") or {}).get("event_id") or "")
        if mtype in ("m.image", "m.video", "m.audio", "m.file"):
            info = c.get("info") or {}
            inc.kind = mtype[2:]
            inc.mxc = c.get("url", "")
            inc.filename = c.get("filename") or body or "file"
            inc.mimetype = info.get("mimetype", "")
            inc.voice = mtype == "m.audio"  # iMessage audio messages are voice notes
            # With a separate filename, body is the caption; otherwise it's the name.
            inc.body = body if c.get("filename") and body != c.get("filename") else ""
        elif body.strip(" ￼") == "" and "￼" in body:
            inc.kind = "unreadable"  # Find My location, app cards: the bridge can't read them
        return inc


class IMessageBot:
    """The python-telegram-bot Bot methods the crew code calls, over iMessage.
    chat_id is a Matrix room id."""

    SOURCE = "iMessage"

    def __init__(self, im: IMessage):
        self.im = im

    async def _prefixed(self, chat_id: str, text: str) -> str:
        # No name prefixes: each bot gets its own thread.
        return text

    def _from(self, content: dict) -> dict:
        """Tag a message with this bot's own address (its thread on the owner's phone)."""
        if self.im.via:
            content[SEND_FROM_KEY] = self.im.via
        return content

    def _remember(self, event_id: str) -> str:
        if not self.im.remember_sends:
            return event_id  # notify.py: not a turn of the bot's own
        self.im.sent_events = (self.im.sent_events + [event_id])[-300:]
        try:
            self.im.mx._save_state(sent_events=self.im.sent_events)
        except OSError:
            pass  # bookkeeping only; never fail a send over it
        return event_id

    async def send_message(self, chat_id: str, text: str, **_) -> str:
        # Links go out as their own bubbles, the way an iPhone splits them, so
        # the bridge sends a rich link preview instead of a raw URL mid-text.
        # [EFFECT: slam] rides on the first bubble; **bold** and friends and
        # [SHAKE: words] become real iMessage formatting (see style_text).
        effect, text = take_effect(text)
        urgent = bool(URGENT_RE.search(text))
        text = URGENT_RE.sub("", text).strip()
        last = self.__dict__.setdefault("_last_text", {})  # room -> (event id, sent at)
        if UNSEND_RE.match(text):
            text = UNSEND_RE.sub("", text, count=1).strip()
            prev = last.pop(chat_id, None)
            if prev and time.time() - prev[1] < 110:  # Apple's window is 2 minutes
                try:
                    await self.im.mx.redact(chat_id, prev[0])
                except OSError as e:
                    log.warning("unsend failed: %s", e)
            if not text:
                return prev[0] if prev else ""
        if EDIT_RE.match(text):
            text = EDIT_RE.sub("", text, count=1).strip()
            prev = last.get(chat_id)
            if prev and text and time.time() - prev[1] < 14 * 60 and not LINK_RE.search(text):
                plain, formatted = style_text(text)
                new = {"msgtype": "m.text", "body": plain}
                if formatted:
                    new.update(format="org.matrix.custom.html", formatted_body=formatted)
                content = self._from({"msgtype": "m.text", "body": "* " + plain, "m.new_content": new,
                                      "m.relates_to": {"rel_type": "m.replace", "event_id": prev[0]}})
                try:
                    return self._remember(await self.im.mx.send_event(chat_id, content))
                except OSError as e:
                    log.warning("edit failed, sending it as a new text: %s", e)
            # Too old, nothing to edit, or it has a link: just send it.
        event_id = ""
        for part in split_links(text):
            plain, formatted = style_text(part)
            if not plain.strip():
                continue
            content = self._from({"msgtype": "m.text", "body": await self._prefixed(chat_id, plain)})
            if formatted and content["body"] == plain:
                content["format"] = "org.matrix.custom.html"
                content["formatted_body"] = formatted
            if effect:
                content["com.atlas.effect"] = effect
                effect = ""
            if urgent:  # on the first bubble: that's what breaks through
                content["com.atlas.notify_anyway"] = True
                urgent = False
            reply_to = self.im.reply_next.pop(chat_id, "")
            if reply_to:  # an iMessage inline (swipe) reply, on the first bubble only
                content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
            event_id = self._remember(await self.im.mx.send_event(chat_id, content))
            if not LINK_RE.search(plain):
                last[chat_id] = (event_id, time.time())
        return event_id

    async def react(self, chat_id: str, event_id: str, key: str) -> None:
        """An iMessage tapback on a message (❤️ 👍 👎 😂 ‼️ ❓ or any emoji)."""
        await self.im.mx.send_event(chat_id, {"m.relates_to": {
            "rel_type": "m.annotation", "event_id": event_id, "key": key}}, etype="m.reaction")

    async def _send_file(self, chat_id: str, fh, msgtype: str, voice: bool = False, **_) -> str:
        path = Path(getattr(fh, "name", ""))
        if not path.is_file():
            raise IMessageError(f"not a file: {path}")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        temp = None
        if voice:
            # The bridge turns OGG Opus into the voice memo an iPhone plays
            # inline. Anything else (mp3, wav, m4a) used to go out labelled
            # audio/ogg anyway and arrive as a broken file, so
            # convert first; the copy is deleted right after.
            if path.suffix.lower() not in (".ogg", ".opus"):
                temp = await _to_ogg_opus(path)
                if temp is None:
                    raise IMessageError(f"couldn't turn {path.name} into a voice note")
                path = temp
            mime = "audio/ogg"
        try:
            mxc = await self.im.mx.upload(path, mime)
            content = {"msgtype": msgtype, "body": path.name, "filename": path.name, "url": mxc,
                       "info": {"mimetype": mime, "size": path.stat().st_size}}
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
        if voice:
            content["org.matrix.msc3245.voice"] = {}
            content["org.matrix.msc1767.audio"] = {}
        return self._remember(await self.im.mx.send_event(chat_id, self._from(content)))

    async def send_photos(self, chat_id, paths) -> str:
        """Several pictures as ONE iMessage stack: the first goes out as a
        normal image and the bridge (stack.go) adds the rest to the same
        message from "com.atlas.stack"."""
        paths = [Path(p) for p in paths][:10]
        extra = []
        for p in paths[1:]:
            mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
            extra.append({"url": await self.im.mx.upload(p, mime), "mimetype": mime, "filename": p.name})
        first = paths[0]
        mime = mimetypes.guess_type(first.name)[0] or "image/jpeg"
        mxc = await self.im.mx.upload(first, mime)
        content = {"msgtype": "m.image", "body": first.name, "filename": first.name, "url": mxc,
                   "info": {"mimetype": mime, "size": first.stat().st_size},
                   "com.atlas.stack": extra}
        return self._remember(await self.im.mx.send_event(chat_id, self._from(content)))

    async def send_photo(self, chat_id, fh, **kw):
        return await self._send_file(chat_id, fh, "m.image", **kw)

    async def send_video(self, chat_id, fh, **kw):
        return await self._send_file(chat_id, fh, "m.video", **kw)

    async def send_audio(self, chat_id, fh, **kw):
        return await self._send_file(chat_id, fh, "m.audio", **kw)

    async def send_voice(self, chat_id, fh, **kw):
        return await self._send_file(chat_id, fh, "m.audio", voice=True, **kw)

    async def send_document(self, chat_id, fh, filename: str = "", **kw):
        return await self._send_file(chat_id, fh, "m.file", **kw)

    async def send_chat_action(self, chat_id, action=None, **_) -> None:
        try:
            await self.im.read_first(chat_id)  # no dots before "Read"
            await self.im.mx.typing(chat_id, True, 8000)
        except Exception:  # typing dots are cosmetic; never fail a turn over them
            pass

    async def stop_typing(self, chat_id) -> None:
        """Clear the dots the moment a turn ends (iMessage keeps showing them
        until it's told otherwise)."""
        try:
            await self.im.mx.typing(chat_id, False)
        except Exception:
            pass


# ------------------------------------------------------------- reactions ----
# A bot acknowledges with a real iMessage tapback by putting [REACT: 👍] (or a
# name like heart / haha / ?) anywhere in its reply. It lands on the newest
# message from the person it's answering. Telegram has no tapbacks, so there
# the emoji is sent as text if nothing else was said.

REACT_RE = re.compile(r"\[REACT:\s*([^\]]+?)\s*\]")
REPLY_RE = re.compile(r"\[REPLY(?::\s*([^\]]*?))?\s*\]\s*", re.I)  # make the text an inline reply to their message
REACT_ALIASES = {
    "heart": "❤️", "love": "❤️", "like": "👍", "thumbs up": "👍", "thumbsup": "👍",
    "dislike": "👎", "thumbs down": "👎", "haha": "😂", "laugh": "😂", "lol": "😂",
    "emphasize": "‼️", "!!": "‼️", "question": "❓", "?": "❓",
}


def drop_reaction_tags(reply: str) -> str:
    """Strip [REACT: ...] and [REPLY] from a BOT-INITIATED message (check-in,
    bus request, brief). There's no incoming message to tapback or swipe-reply
    to, so apply_reactions never runs on these - the tags would otherwise go
    out as literal text, e.g. a literal "[REACT: 👍]"."""
    text = REPLY_RE.sub("", REACT_RE.sub("", reply))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


QUOTE_CHARS = "\"'“”"  # straight and curly quotes around a quoted message


def find_target(im, chat_id, quote: str) -> str:
    """The message a tapback or swipe-reply should land on: the newest one
    whose text contains `quote` (case and punctuation don't matter), else the
    newest message. Lets a bot answer something from a few texts back, the
    way a person scrolls up and taps it."""
    newest = im.last_event.get(chat_id, "") if im else ""
    if not quote or not im:
        return newest
    def norm(t: str) -> str:
        return " ".join(re.sub(r"[^\w\s]", " ", t.lower()).split())
    want = norm(quote)
    if not want:
        return newest
    for event_id, body in reversed(im.recent_in.get(chat_id, [])):
        if want in norm(body):
            return event_id
    return newest


async def apply_reactions(bot, chat_id, reply: str) -> str:
    """Send any [REACT: ...] tags as tapbacks, turn [REPLY] into an inline
    reply to the message being answered; return the rest of the reply.
    Both take an optional quote of the message they're for:
    [REACT: 😂 | "words from their text"], [REPLY: "words from their text"]."""
    im = getattr(bot, "im", None)
    m = REPLY_RE.search(reply or "")
    if m:
        reply = REPLY_RE.sub("", reply).strip()
        target = find_target(im, chat_id, (m.group(1) or "").strip().strip(QUOTE_CHARS))
        if target:
            im.reply_next[chat_id] = target
    tags = REACT_RE.findall(reply or "")
    if not tags:
        return reply
    reply = REACT_RE.sub("", reply).strip()
    plan = []
    for t in tags:
        emoji, _, quote = t.partition("|")
        emoji = emoji.strip()
        plan.append((REACT_ALIASES.get(emoji.lower(), emoji), quote.strip().strip(QUOTE_CHARS)))
    if not (im and im.last_event.get(chat_id)):  # Telegram, or nothing to react to
        return reply or " ".join(k for k, _ in plan)
    for key, quote in plan:
        try:
            await bot.react(chat_id, find_target(im, chat_id, quote), key)
        except OSError as e:
            log.warning("reaction %s failed: %s", key, e)
    return reply


# Only what the bots need: messages and room state, no presence noise.
_FILTER = json.dumps({
    "presence": {"types": []},
    "account_data": {"types": []},
    "room": {
        "timeline": {"limit": 50, "types": ["m.room.message"]},
        "state": {"types": ["m.bridge", "uk.half-shot.bridge"], "lazy_load_members": True},
        "ephemeral": {"types": []},
        "account_data": {"types": []},
    },
})
