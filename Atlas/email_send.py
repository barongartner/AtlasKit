"""Send an email from the owner's Gmail. The draft must be approved in chat first.

Usage: python email_send.py to@example.com "Subject" body.txt
"""

import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env", override=True)  # the bot's stale env copy must not beat a freshly rotated password
sys.path.insert(0, str(BASE.parent / "Shared"))
import owner  # noqa: E402

ADDRESS = os.getenv("GMAIL_ADDRESS", "").strip() or owner.EMAIL
APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")


def main() -> None:
    # An emoji in the subject used to crash the "Sent" line AFTER the mail had
    # gone out, which reads as a failure and invites a second send.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not ADDRESS or not APP_PASSWORD:
        sys.exit("Gmail creds missing from .env (see email_check.py for setup).")
    if len(sys.argv) < 4:
        sys.exit('Usage: python email_send.py to@example.com "Subject" body.txt')
    to, subject, body_file = sys.argv[1], sys.argv[2], sys.argv[3]
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = ADDRESS, to, subject
    msg.set_content(Path(body_file).read_text(encoding="utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(ADDRESS, APP_PASSWORD)
        smtp.send_message(msg)
    print(f"Sent to {to}: {subject}")


if __name__ == "__main__":
    main()
