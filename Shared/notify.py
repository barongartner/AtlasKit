r"""Text the owner over iMessage from outside a bot's turn (long-job "done" pings).

    python Shared\notify.py Atlas "render finished"

Uses that bot's .env and Shared's .env (MATRIX_TOKEN etc.) and its
imessage_state.json to find the thread the owner last texted it in, so the
message lands where they expect.
Chain it after a long command:  <job> ; python Shared\notify.py Atlas "job done"

Quiet hours (QUIET_HOURS in the bot's .env or Shared's .env, default
23:00-08:00): only Atlas may text the owner, so any other bot's message goes
on Atlas's bus instead.
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import owner  # noqa: E402
from bus import send as bus_send  # noqa: E402
from imessage import IMessage  # noqa: E402


def in_quiet_hours(spec: str) -> bool:
    try:
        start, end = ((int(h) * 60 + int(m)) for h, m in (p.split(":") for p in spec.split("-")))
    except ValueError:
        start, end = 23 * 60, 8 * 60
    now = datetime.now().hour * 60 + datetime.now().minute
    return start <= now or now < end if start > end else start <= now < end


async def main(bot: str, text: str) -> None:
    house = HERE.parent / bot
    env = dotenv_values(house / ".env")
    quiet = env.get("QUIET_HOURS") or owner.get("QUIET_HOURS", "23:00-08:00")
    if bot != "Atlas" and in_quiet_hours(quiet):
        bus_send(bot, "Atlas", f"Held during quiet hours (I can't text {owner.NAME} at night). "
                 "Pass it on now only if it genuinely can't wait: " + text)
        print("quiet hours - handed to Atlas")
        return
    if not IMessage.configured(env):
        sys.exit(f"{bot} has no MATRIX_TOKEN in its .env or Shared's .env, so it can't text over iMessage.")
    im = IMessage(bot, dict(env), house / "imessage_state.json")
    # Not one of the bot's own turns: leave it off the bot's sent list so its
    # next turn hears about this text (the bot reads its thread for these).
    im.remember_sends = False
    room = im.home_room()
    if not room:
        sys.exit(f"{bot} doesn't know {owner.NAME}'s iMessage thread yet.")
    try:
        await im.mx.whoami()
        await im.bot.send_message(room, text)
    finally:
        await im.mx.http.aclose()
    print("sent")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], " ".join(sys.argv[2:])))
