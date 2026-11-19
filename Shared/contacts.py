r"""Look up the owner's Apple contacts (their main iCloud account, read-only).

Uses the same APPLE_ID / APPLE_APP_PASSWORD as the calendar scripts, read from
Atlas's .env or Shared's .env (setup writes them). Talks CardDAV to contacts.icloud.com. Nothing is cached to disk:
every lookup asks iCloud, so it's always current and no phone numbers end up
in backups.

    python contacts.py find Max         contacts whose name, nickname or company match
    python contacts.py find 555123      contacts with that number fragment
    python contacts.py count            how many contacts there are

Any bot can use it; Iris uses it to find who to text.
"""

import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import owner  # noqa: E402

if (HERE.parent / "Atlas" / ".env").exists():
    load_dotenv(HERE.parent / "Atlas" / ".env", override=True)

APPLE_ID = os.getenv("APPLE_ID", "").strip() or owner.get("APPLE_ID")
APP_PASSWORD = (os.getenv("APPLE_APP_PASSWORD", "").strip() or owner.get("APPLE_APP_PASSWORD")).replace(" ", "")
ROOT = "https://contacts.icloud.com"
NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:carddav"}


def _dav(method: str, url: str, body: str, depth: str = "0") -> ET.Element:
    r = requests.request(method, url, data=body.encode(), auth=(APPLE_ID, APP_PASSWORD),
                         headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
                         timeout=60)
    if r.status_code == 401:
        sys.exit("iCloud refused the login: the app password in Atlas's .env is wrong or revoked.")
    r.raise_for_status()
    return ET.fromstring(r.content)


def _href(tree: ET.Element, prop: str) -> str:
    el = tree.find(f".//d:{prop}/d:href", NS) if prop != "addressbook-home-set" else tree.find(".//c:addressbook-home-set/d:href", NS)
    if el is None:
        sys.exit(f"iCloud didn't return {prop}.")
    return el.text.strip()


def _abs(base: str, href: str) -> str:
    return href if href.startswith("http") else re.match(r"https://[^/]+", base).group(0) + href


def fetch_cards() -> list[str]:
    if not APPLE_ID or not APP_PASSWORD:
        sys.exit("Contacts not connected: APPLE_ID / APPLE_APP_PASSWORD missing from Atlas's .env.")
    prop = '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav"><d:prop>{}</d:prop></d:propfind>'
    principal = _abs(ROOT, _href(_dav("PROPFIND", ROOT + "/", prop.format("<d:current-user-principal/>")), "current-user-principal"))
    home = _abs(principal, _href(_dav("PROPFIND", principal, prop.format("<c:addressbook-home-set/>")), "addressbook-home-set"))
    books = _dav("PROPFIND", home, prop.format("<d:resourcetype/>"), depth="1")
    cards = []
    for resp in books.findall("d:response", NS):
        if resp.find(".//c:addressbook", NS) is None:
            continue
        url = _abs(home, resp.find("d:href", NS).text.strip())
        report = ('<c:addressbook-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
                  '<d:prop><c:address-data/></d:prop></c:addressbook-query>')
        for data in _dav("REPORT", url, report, depth="1").findall(".//c:address-data", NS):
            if data.text and "BEGIN:VCARD" in data.text:
                cards.append(data.text)
    return cards


def parse(card: str) -> dict:
    card = re.sub(r"\r?\n[ \t]", "", card)  # unfold continuation lines
    c = {"name": "", "nickname": "", "org": "", "phones": [], "emails": [], "birthday": ""}
    for line in card.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        name = key.split(";")[0].split(".")[-1].upper()
        label = ",".join(t.lower() for t in re.findall(r"TYPE=([^;:]+)", key, re.I) if t.lower() not in ("pref", "voice", "internet"))
        val = val.replace("\\,", ",").replace("\\;", ";").strip()
        if name == "FN":
            c["name"] = val
        elif name == "NICKNAME":
            c["nickname"] = val
        elif name == "ORG":
            c["org"] = val.strip(";")
        elif name == "TEL":
            c["phones"].append((label, val))
        elif name == "EMAIL":
            c["emails"].append((label, val))
        elif name == "BDAY":
            c["birthday"] = val
    return c


def show(c: dict) -> str:
    out = [c["name"] or "(no name)"]
    if c["nickname"]:
        out[0] += f' ("{c["nickname"]}")'
    if c["org"]:
        out.append(f"  company: {c['org']}")
    out += [f"  phone{' (' + l + ')' if l else ''}: {v}" for l, v in c["phones"]]
    out += [f"  email{' (' + l + ')' if l else ''}: {v}" for l, v in c["emails"]]
    if c["birthday"]:
        out.append(f"  birthday: {c['birthday']}")
    return "\n".join(out)


def find(query: str) -> list[dict]:
    q = query.lower()
    digits = re.sub(r"\D", "", query)
    hits = []
    for c in map(parse, fetch_cards()):
        text = f"{c['name']} {c['nickname']} {c['org']}".lower()
        nums = [re.sub(r"\D", "", v) for _, v in c["phones"]]
        if q in text or (len(digits) >= 3 and any(digits in n for n in nums)):
            hits.append(c)
    return hits


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) >= 2 and sys.argv[1] == "count":
        print(len(fetch_cards()), "contacts")
    elif len(sys.argv) >= 3 and sys.argv[1] == "find":
        hits = find(" ".join(sys.argv[2:]))
        print("\n\n".join(map(show, hits)) if hits else "no match")
    else:
        sys.exit(__doc__)
