"""List real human emails from Gmail for Atlas.

Usage: python email_check.py [hours]   (default 24)
Creds: GMAIL_ADDRESS + GMAIL_APP_PASSWORD in .env (app password from
myaccount.google.com/apppasswords - requires 2FA on the Google account).
Read-only: nothing gets marked as read.
"""

import email
import email.header
import email.utils
import imaplib
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env", override=True)
sys.path.insert(0, str(BASE.parent / "Shared"))
import owner  # noqa: E402

ADDRESS = os.getenv("GMAIL_ADDRESS", "").strip() or owner.EMAIL
APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")

BULK_SENDER = re.compile(
    r"no-?reply|donotreply|notifications?@|newsletter|marketing|mailer@|"
    r"@(?:facebookmail|linkedin|twitter|x|tiktok|youtube|google|apple|amazon|"
    r"instagram|discord|steampowered|spotify|netflix|paypal|reddit|epicgames)\.",
    re.I,
)


def decode(value: str) -> str:
    out = ""
    for text, enc in email.header.decode_header(value or ""):
        out += text.decode(enc or "utf-8", "replace") if isinstance(text, bytes) else text
    return " ".join(out.split())


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    if not ADDRESS or not APP_PASSWORD:
        sys.exit(
            "Gmail not connected: GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing from "
            f".env.\n{owner.NAME} makes an app password at myaccount.google.com/apppasswords "
            "(needs 2FA on), then texts it to Atlas."
        )
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    days = max(1, -(-hours // 24))

    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(ADDRESS, APP_PASSWORD)
    imap.select("INBOX", readonly=True)
    # Gmail's own Primary-tab classification does the heavy junk lifting.
    status, data = imap.search(None, "X-GM-RAW", f'"category:primary newer_than:{days}d"')
    ids = data[0].split() if status == "OK" else []

    cutoff = datetime.now().astimezone() - timedelta(hours=hours)
    shown = 0
    for msg_id in reversed(ids[-40:]):
        status, msg_data = imap.fetch(msg_id, "(BODY.PEEK[HEADER])")
        if status != "OK" or not msg_data or msg_data[0] is None:
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        sender = decode(msg.get("From", ""))
        if msg.get("List-Unsubscribe") or BULK_SENDER.search(sender):
            continue
        try:
            sent_at = email.utils.parsedate_to_datetime(msg.get("Date", ""))
            if sent_at and sent_at.astimezone() < cutoff:
                continue
        except (TypeError, ValueError):
            pass
        subject = decode(msg.get("Subject", "")) or "(no subject)"
        # The owner's local time, 12-hour, not the sender's own clock.
        when = decode(msg.get("Date", ""))[:22]
        try:
            local = email.utils.parsedate_to_datetime(msg.get("Date", "")).astimezone(owner.TZ)
            when = f"{local:%a %b} {local.day} " + local.strftime("%I:%M %p").lstrip("0")
        except (TypeError, ValueError):
            pass
        print(f"{sender} | {subject} | {when}")
        shown += 1
    imap.logout()
    if not shown:
        print(f"No real human mail in the last {hours}h. Inbox is all noise.")


if __name__ == "__main__":
    main()
