"""Who this install belongs to, read from Shared\\.env (setup writes it).

Every script and bot gets the owner's details from here. Nothing about a
specific person is written into the code: no names, numbers, cities or time
zones. Import it the way the bots import the rest of Shared:

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Shared"))
    import owner
    owner.NAME, owner.PHONE, owner.TZ ...
"""

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values

SHARED = Path(__file__).resolve().parent
ROOT = SHARED.parent  # the kit's folder, wherever it was installed
ENV_FILE = SHARED / ".env"

_env = {k: (v or "").strip() for k, v in dotenv_values(ENV_FILE).items()} if ENV_FILE.exists() else {}


def get(name: str, default: str = "") -> str:
    """A Shared\\.env value, with the process environment as the fallback."""
    return _env.get(name) or os.getenv(name, "").strip() or default


NAME = get("OWNER_NAME", "the owner")  # first name, used in prompts and texts
PHONE = get("OWNER_PHONE")  # E.164, e.g. +15551234567; blank if they text from an email
EMAIL = get("OWNER_EMAIL")  # their own address (Gmail checks, calendar)


def _handle(raw: str) -> str:
    """A phone number or email in the bridge's spelling: tel:+1555... or mailto:x@y."""
    r = raw.strip()
    if not r:
        return ""
    if r.lower().startswith(("tel:", "mailto:")):
        kind, _, rest = r.partition(":")
        r = rest if kind.lower() == "tel" else r
    if "@" in r:
        return "mailto:" + r.split(":", 1)[-1].lower()
    digits = "".join(c for c in r if c.isdigit())
    return f"tel:+{digits}" if len(digits) >= 8 else ""


# Every iMessage handle the owner texts from: their phone number and/or the
# Apple ID email(s) in OWNER_IMESSAGE. Only these can give the bots orders.
HANDLES = ",".join(dict.fromkeys(h for h in map(_handle, [PHONE] + get("OWNER_IMESSAGE").split(",")) if h))
CITY = get("OWNER_CITY")  # weather and local news
TZ_NAME = get("OWNER_TIMEZONE", "America/Toronto")  # IANA name
BOT_APPLE_ID = get("BOT_APPLE_ID")  # the separate Apple ID the bridge signs in with
WSL_DISTRO = get("WSL_DISTRO", "Ubuntu-24.04")
MATRIX_SERVER = get("MATRIX_SERVER_NAME", "atlas.local")

try:
    TZ = ZoneInfo(TZ_NAME)
except ZoneInfoNotFoundError:
    TZ = ZoneInfo("UTC")


def bot_address(bot: str) -> str:
    """The bot's own iMessage address: the bot Apple ID with a +bot alias,
    so each bot shows up as its own conversation on the owner's phone."""
    user, sep, domain = BOT_APPLE_ID.partition("@")
    return f"{user}+{bot.lower()}@{domain}" if sep else ""


def missing() -> list[str]:
    """The settings setup still has to fill in."""
    need = {"OWNER_NAME": NAME != "the owner", "OWNER_PHONE": HANDLES,
            "OWNER_TIMEZONE": get("OWNER_TIMEZONE"), "BOT_APPLE_ID": BOT_APPLE_ID}
    return [k for k, ok in need.items() if not ok]
