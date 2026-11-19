"""Iris's own email (the IRIS_EMAIL_ADDRESS Gmail account, sender name "Iris").

Usage:
  python email_iris.py send to@example.com "Subject" body.txt [--cc a@b.com] [--attach file]
  python email_iris.py inbox [hours]       mail Iris received (default last 24h), newest first
  python email_iris.py read N              full text of message N from the last inbox list
Settings (Shared\\.env, written by setup, or this folder's .env, which wins):
IRIS_EMAIL_ADDRESS (what recipients see) and IRIS_EMAIL_APP_PASSWORD. For an iCloud
alias (@icloud.com) also IRIS_EMAIL_LOGIN, the main iCloud Mail address the alias
belongs to, and the password is an Apple app-specific password; iCloud's servers are
used and the inbox is filtered to mail sent to the alias. For Gmail the password is a
Google app password and the login is the address itself.
"""

import email
import imaplib
import json
import mimetypes
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
import email.policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, parsedate_to_datetime
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env", override=True)
sys.path.insert(0, str(BASE.parent / "Shared"))
import owner  # noqa: E402


def setting(name: str) -> str:
    """This folder's .env first, then Shared\\.env."""
    return os.getenv(name, "").strip() or owner.get(name)


ADDRESS = setting("IRIS_EMAIL_ADDRESS")
APP_PASSWORD = setting("IRIS_EMAIL_APP_PASSWORD").replace(" ", "")
# An iCloud alias logs in as the main iCloud account but sends as the alias.
LOGIN = setting("IRIS_EMAIL_LOGIN") or ADDRESS
ICLOUD = ADDRESS.lower().endswith(("@icloud.com", "@me.com", "@mac.com"))
SMTP_HOST, IMAP_HOST = ("smtp.mail.me.com", "imap.mail.me.com") if ICLOUD else ("smtp.gmail.com", "imap.gmail.com")
LAST_LIST = BASE / "Inbox" / "email_last_list.json"  # maps N -> IMAP uid for `read`


def need_creds() -> None:
    if not ADDRESS or not APP_PASSWORD:
        sys.exit("Iris email not set up: IRIS_EMAIL_ADDRESS / IRIS_EMAIL_APP_PASSWORD missing. "
                 "Add them in the Atlas app's setup (the optional email step). A Gmail app "
                 "password is made at myaccount.google.com/apppasswords while signed in as that account.")


def pop_all(args: list[str], name: str) -> list[str]:
    vals = []
    while name in args:
        i = args.index(name)
        vals.append(args[i + 1])
        del args[i:i + 2]
    return vals


def send(args: list[str]) -> None:
    cc, attach = pop_all(args, "--cc"), pop_all(args, "--attach")
    to, subject, body_file = args[0], args[1], args[2]
    msg = EmailMessage()
    msg["From"] = formataddr(("Iris", ADDRESS))
    msg["To"], msg["Subject"] = to, subject
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.set_content(Path(body_file).read_text(encoding="utf-8"))
    for f in map(Path, attach):
        kind = (mimetypes.guess_type(f.name)[0] or "application/octet-stream").split("/")
        msg.add_attachment(f.read_bytes(), maintype=kind[0], subtype=kind[1], filename=f.name)
    if ICLOUD:  # iCloud only takes STARTTLS on 587
        with smtplib.SMTP(SMTP_HOST, 587) as smtp:
            smtp.starttls()
            smtp.login(LOGIN, APP_PASSWORD)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP_SSL(SMTP_HOST, 465) as smtp:
            smtp.login(LOGIN, APP_PASSWORD)
            smtp.send_message(msg)
    print(f"Sent to {to}{' (cc ' + ', '.join(cc) + ')' if cc else ''}: {subject}")


def text(v) -> str:
    return str(make_header(decode_header(v or "")))


def body_of(m: email.message.Message) -> str:
    part = m.get_body(preferencelist=("plain", "html")) if hasattr(m, "get_body") else m
    return (part.get_content() if part else "").strip()


def inbox(args: list[str]) -> None:
    hours = int(args[0]) if args else 24
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%d-%b-%Y")
    with imaplib.IMAP4_SSL(IMAP_HOST) as imap:
        imap.login(LOGIN, APP_PASSWORD)
        imap.select("INBOX", readonly=True)
        # On an iCloud alias the inbox is the main account's: only mail sent to Iris counts.
        query = f'(SINCE "{since}" TO "{ADDRESS}")' if ICLOUD else f'(SINCE "{since}")'
        _, data = imap.uid("search", None, query)
        uids = data[0].split()[::-1][:30]
        rows, cutoff = [], datetime.now(timezone.utc) - timedelta(hours=hours)
        for uid in uids:
            _, msg = imap.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            h = email.message_from_bytes(msg[0][1])
            when = parsedate_to_datetime(h["Date"]) if h["Date"] else None
            if when and when < cutoff:
                continue
            rows.append((uid.decode(), when, text(h["From"]), text(h["Subject"])))
    LAST_LIST.parent.mkdir(exist_ok=True)
    LAST_LIST.write_text(json.dumps([r[0] for r in rows]))
    if not rows:
        print(f"No mail in the last {hours}h.")
    for n, (_, when, frm, subj) in enumerate(rows, 1):
        stamp = when.astimezone().strftime("%a %b %d %I:%M %p") if when else "?"
        print(f"{n}. {stamp} | {frm} | {subj}")


def read(args: list[str]) -> None:
    uids = json.loads(LAST_LIST.read_text()) if LAST_LIST.exists() else []
    n = int(args[0])
    if not 1 <= n <= len(uids):
        sys.exit("Run `inbox` first, then read a number from that list.")
    with imaplib.IMAP4_SSL(IMAP_HOST) as imap:
        imap.login(LOGIN, APP_PASSWORD)
        imap.select("INBOX", readonly=True)
        _, msg = imap.uid("fetch", uids[n - 1], "(BODY.PEEK[])")
    m = email.message_from_bytes(msg[0][1], policy=email.policy.default)
    print(f"From: {m['From']}\nTo: {m['To']}\nDate: {m['Date']}\nSubject: {m['Subject']}\n")
    print(body_of(m))


def summarize(raw: bytes, limit: int = 3000) -> str:
    m = email.message_from_bytes(raw, policy=email.policy.default)
    body = body_of(m)
    if len(body) > limit:
        body = body[:limit] + "\n[...cut]"
    return (f"From: {m['From']}\nTo: {m['To']}\nDate: {m['Date']}\n"
            f"Subject: {m['Subject']}\n\n{body}")


def idle_forever(on_new, log=None) -> None:
    """Block forever with an IMAP IDLE connection: the mail server pushes new
    mail the moment it lands, and on_new(list of summaries) runs for it. No
    polling. Reconnects on any error; re-IDLEs every 25 min (servers drop ~29).
    The wait uses select(), not a socket timeout: a timeout poisons the socket
    ("cannot read from timed out object") and drops the connection on every
    25-minute cycle."""
    import select
    import time
    last_uid = None
    dropped = False
    while True:
        try:
            with imaplib.IMAP4_SSL(IMAP_HOST, timeout=60) as imap:
                imap.login(LOGIN, APP_PASSWORD)
                imap.select("INBOX", readonly=True)
                if dropped and log:
                    log.info("Email push on for %s (reconnected)", ADDRESS)
                dropped = False
                if last_uid is None:  # start from what's there now: old mail isn't new
                    _, data = imap.uid("search", None, "ALL")
                    last_uid = int(data[0].split()[-1]) if data[0] else 0
                while True:
                    _, data = imap.uid("search", None, f"UID {last_uid + 1}:*")
                    uids = [int(u) for u in data[0].split() if int(u) > last_uid]
                    if uids:
                        found = []
                        for uid in uids:
                            _, msg = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
                            found.append(summarize(msg[0][1]))
                        last_uid = max(uids)
                        on_new(found)
                    tag = imap._new_tag().decode()
                    imap.send(f"{tag} IDLE\r\n".encode())
                    imap.readline()  # "+ idling"
                    # Wait for the server to push something, or 25 min to pass.
                    if imap.sock.pending() or select.select([imap.sock], [], [], 25 * 60)[0]:
                        imap.readline()
                    imap.send(b"DONE\r\n")
                    while not imap.readline().startswith(tag.encode()):
                        pass
        except Exception as e:  # noqa: BLE001 - network blips: reconnect
            dropped = True
            if log:
                log.warning("email push connection dropped (%s), reconnecting in 30s", e)
            time.sleep(30)


def main(argv: list[str]) -> None:
    if not argv or argv[0] not in ("send", "inbox", "read"):
        sys.exit(__doc__)
    need_creds()
    {"send": send, "inbox": inbox, "read": read}[argv[0]](argv[1:])


if __name__ == "__main__":
    main(sys.argv[1:])
