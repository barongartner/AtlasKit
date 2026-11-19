"""Fast lane for time-sensitive email: the email watches in watch.py's store.

atlas_bot.py calls check() the moment Gmail pushes new mail (idle_forever), plus a
safety sweep every WATCH_MINUTES (default 30), day and night.
Pure IMAP + string matching - no Claude turn, so it costs nothing to run
often. Unlike email_check.py it does NOT filter bulk senders or non-Primary
tabs: a watch is an explicit "tell me when THIS lands", so an automated
mailer@ notice counts.

Watches are added with `python watch.py add email <match>`. The match is
checked case-insensitively against the From header (name + address), or
against the subject for a `subject:` watch. Mail sent before the watch was
set doesn't count. A hit is stamped onto its watch (last fired).

Manual test: python email_watch.py   (prints hits, marks nothing as seen)
"""

import email
import email.header
import email.message
import email.utils
import html
import imaplib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import sys

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent / "Shared"))
import owner  # noqa: E402
import watch  # noqa: E402  (the watch store; beside this file)

SEEN_FILE = BASE / "watch_seen.json"
LOOKBACK_DAYS = 2  # IMAP window; seen-set stops repeats inside it
FIRST_RUN_GRACE = timedelta(hours=3)  # first-ever run: don't dump old mail


def decode(value: str) -> str:
    out = ""
    for text, enc in email.header.decode_header(value or ""):
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", "replace")
        else:
            out += text
    return " ".join(out.split())


def gmail_creds() -> tuple[str, str]:
    """(address, app password). The address falls back to the owner's email."""
    address = os.getenv("GMAIL_ADDRESS", "").strip() or owner.EMAIL
    password = os.getenv("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
    return address, password


def load_watches() -> list[tuple[str, str, str]]:
    """[(field, needle, label)] for every active email watch (the app's
    Email page tags matching mail with these)."""
    return [(r["field"], r["needle"], r["label"]) for r in watch.email_rows()]


def _set_before(sent_header: str, created) -> bool:
    """True when the mail was sent more than 10 minutes before the watch
    existed: "tell me when X replies" means from now on."""
    made = watch._parse_dt(created)
    if not made:
        return False
    try:
        sent = email.utils.parsedate_to_datetime(sent_header or "")
    except (TypeError, ValueError):
        return False
    if not sent:
        return False
    return sent.astimezone() < made.astimezone() - timedelta(minutes=10)


def _html_text(raw: str) -> str:
    """Rough text out of an HTML body, one line per row/paragraph."""
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</div>|</li>|</h\d>", "\n", raw)
    return html.unescape(re.sub(r"<[^>]+>", " ", raw))


def _snippet(msg: email.message.Message, limit: int = 220, joiner: str = " ") -> str:
    """Start of the body. Some senders mail HTML only, so without the HTML
    fallback those hits would arrive with no details at all."""
    plain = html_body = ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_filename():
            continue
        kind = part.get_content_type()
        if kind not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if kind == "text/plain" and not plain:
            plain = text
        elif kind == "text/html" and not html_body:
            html_body = _html_text(text)
    part_text = plain if plain.strip() else html_body
    kept = []
    for ln in part_text.splitlines():
        s = ln.strip()
        if s.startswith(">") or re.match(r"^On .+ wrote:$", s):
            break  # stop at the quoted thread
        if s:
            kept.append(" ".join(s.split()))
    text = joiner.join(kept)
    return text[:limit] + ("..." if len(text) > limit else "")


def idle_forever(on_push, log=None) -> None:
    """Block forever on an IMAP IDLE connection to the owner's Gmail and call
    on_push() the moment the server says new mail landed, so the bot runs
    check() right then instead of on a timer. Re-IDLEs every 25 min (Gmail
    drops ~29) and reconnects after any error. The wait uses select(), not a
    socket timeout: a timeout poisons the socket ("cannot read from timed out
    object") and drops the connection every 25 min."""
    import select
    import time
    dropped = False
    while True:
        load_dotenv(BASE / ".env", override=True)
        address, password = gmail_creds()
        if not address or not password:
            time.sleep(300)
            continue
        try:
            imap = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
            imap.login(address, password)
            imap.select("INBOX", readonly=True)
            if dropped and log:
                log.info("Gmail push on (reconnected)")
            dropped = False
            while True:
                tag = imap._new_tag().decode()
                imap.send(f"{tag} IDLE\r\n".encode())
                imap.readline()  # "+ idling"
                pushed = False
                # Wait for the server to push something, or 25 min to pass.
                if imap.sock.pending() or select.select([imap.sock], [], [], 25 * 60)[0]:
                    pushed = b"EXISTS" in imap.readline()
                imap.send(b"DONE\r\n")
                while True:
                    line = imap.readline()
                    if line.startswith(tag.encode()):
                        break
                    pushed = pushed or b"EXISTS" in line
                if pushed:
                    on_push()
        except Exception as e:  # noqa: BLE001 - network blips: reconnect
            dropped = True
            if log:
                log.warning("Gmail push connection dropped (%s), reconnecting in 30s", e)
            time.sleep(30)


def check(dry_run: bool = False) -> tuple[list[dict], str]:
    """Return (hits, error). error is "" on success, "nocreds" when Gmail
    isn't set up, or a short message on connection failure."""
    load_dotenv(BASE / ".env", override=True)
    address, password = gmail_creds()
    watches = watch.email_rows()
    if not watches:
        return [], ""
    if not address or not password:
        if not dry_run:
            watch.note_email_sweep("Gmail isn't set up (no app password)")
        return [], "nocreds"

    first_run = not SEEN_FILE.exists()
    try:
        seen = set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        seen = set()

    hits = []
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
        imap.login(address, password)
        imap.select("INBOX", readonly=True)
        since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        status, data = imap.search(None, "SINCE", since)
        ids = data[0].split() if status == "OK" else []
        cutoff = datetime.now().astimezone() - FIRST_RUN_GRACE
        for msg_id in ids[-200:]:
            status, hdr = imap.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
            if status != "OK" or not hdr or hdr[0] is None:
                continue
            h = email.message_from_bytes(hdr[0][1])
            key = (h.get("Message-ID") or f"{h.get('Date')}|{h.get('From')}").strip()
            if key in seen:
                continue
            sender, subject = decode(h.get("From", "")), decode(h.get("Subject", ""))
            row = next(
                (r for r in watches
                 if r["needle"] in (sender if r["field"] == "from" else subject).lower()
                 and not _set_before(h.get("Date", ""), r["created"])),
                None,
            )
            if row is None:
                continue
            label = row["label"]
            if not dry_run:
                seen.add(key)
            if first_run:
                try:
                    sent = email.utils.parsedate_to_datetime(h.get("Date", ""))
                    if sent and sent.astimezone() < cutoff:
                        continue  # pre-existing mail on the very first sweep
                except (TypeError, ValueError):
                    pass
            status, full = imap.fetch(msg_id, "(BODY.PEEK[])")
            snippet = ""
            if status == "OK" and full and full[0] is not None:
                # Long and line-broken: Atlas reads it to pick out the details.
                snippet = _snippet(email.message_from_bytes(full[0][1]), 4000, "\n")
            hits.append({"id": row["id"], "label": label, "from": sender,
                         "subject": subject or "(no subject)", "snippet": snippet})
        imap.logout()
    except (imaplib.IMAP4.error, OSError) as e:
        err = f"{type(e).__name__}: {e}"[:200]
        if not dry_run:
            _stamp(lambda: watch.note_email_sweep(err))
        return [], err

    if not dry_run:
        try:
            SEEN_FILE.write_text(json.dumps(sorted(seen)[-2000:]), encoding="utf-8")
        except OSError:
            pass
        _stamp(lambda: watch.note_email_sweep(""))
        for hit in hits:
            _stamp(lambda: watch.mark_fired(hit["id"], f"From {hit['from']}: {hit['subject']}"))
    return hits, ""


def _stamp(fn) -> None:
    """Bookkeeping on the watch store must never cost a hit."""
    try:
        fn()
    except Exception:  # noqa: BLE001
        pass


def format_hit(hit: dict) -> str:
    who = email.utils.parseaddr(hit["from"])
    name = who[0] or who[1] or hit["from"]
    out = f"📬 {hit['label']}\nFrom {name}: {hit['subject']}"
    if hit["snippet"]:
        short = " ".join(hit["snippet"].split())
        out += f"\n\"{short[:220]}{'...' if len(short) > 220 else ''}\""
    return out


def format_group(label: str, hits: list[dict]) -> str:
    """Multiple hits landing under the same label in one sweep (e.g. several
    notices from one sender) collapse into a single ping instead of one each."""
    if len(hits) == 1:
        return format_hit(hits[0])
    out = f"📬 {label} ({len(hits)} new)"
    for hit in hits:
        who = email.utils.parseaddr(hit["from"])
        name = who[0] or who[1] or hit["from"]
        out += f"\nFrom {name}: {hit['subject']}"
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"Email watches: {[(r['id'], r['field'], r['needle']) for r in watch.email_rows()]}")
    found, err = check(dry_run=True)
    if err:
        print("ERROR:", err)
    for hit in found:
        print(format_hit(hit), "\n")
    if not found and not err:
        print("No new matching mail.")
