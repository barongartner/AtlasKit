r"""Watches: things Atlas keeps an eye on and speaks up about.

One store, Atlas\watches.json (per-user data, gitignored, made on the first
add). Atlas manages it with this file's CLI; the bot's watch loops and the
app's Watches page use it as a library. Nobody edits the JSON by hand.
ATLAS_WATCHES_FILE points the store somewhere else (tests).

Types
  email    a sender or subject match on the owner's Gmail. email_watch.py does
           the IMAP side (Gmail push plus the safety sweep).
  price    a stock (Yahoo Finance's public chart endpoint) or crypto
           (CoinGecko's public API) going above or below a number, or moving
           N% in a day. Fires once per crossing, not on every check.
  web      a page, or one part of it (CSS selector), changing; or some text
           appearing or disappearing on it. Whitespace churn doesn't count.
  youtube  a channel's public RSS feed: fires on a new upload.
  team     a team's games on ESPN's public scoreboard: the final score, and
           the start when asked. F1 watches the race (optionally one driver).

CLI (one plain line per result; exit code 1 when anything failed)
  python watch.py add email <sender | subject:words> [--label L]
  python watch.py add price <SYMBOL> above|below|up|down|move <number> [--crypto | --stock] [--currency cad] [--label L]
  python watch.py add web <url> [--selector CSS] [--text words] [--mode change|appears|disappears] [--label L]
  python watch.py add youtube <channel url | @handle | channel id> [--label L]
  python watch.py add team <league> <team name> [--start] [--label L]
  python watch.py list [--all]
  python watch.py done|pause|resume|remove <id>
  python watch.py check [id]

`check` is a peek: it fetches now and prints what it found, but never uses up
an alert or moves a baseline, so the bot still sends a real fire itself.
"""

import argparse
import copy
import difflib
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests

BASE = Path(__file__).resolve().parent
DEFAULT_STORE = BASE / "watches.json"

TYPES = ("email", "price", "web", "youtube", "team")
STATUSES = ("active", "paused", "done")
# A generic client name. ESPN's edge turns away anything with "bot" in it.
UA = "Mozilla/5.0 (compatible; AtlasWatch/1.0)"
TIMEOUT = (8, 20)  # connect, read
MAX_BYTES = 3_000_000
KEEP_TEXT = 8000  # characters of a web watch's text kept for the "what changed" note
FAIL_ALERT = 24  # consecutive failed checks before the bot is told once

# Seconds until the next check, per type. Prices and teams adjust these to
# market hours and game windows in their own checkers.
INTERVAL = {"price": 300, "web": 900, "youtube": 900, "team": 3600}
ERROR_RETRY = {"price": 600, "web": 900, "youtube": 900, "team": 600}

ESPN = "https://site.api.espn.com/apis/site/v2/sports/"
LEAGUES = {
    "nhl": ("hockey/nhl", "NHL"),
    "nba": ("basketball/nba", "NBA"),
    "wnba": ("basketball/wnba", "WNBA"),
    "nfl": ("football/nfl", "NFL"),
    "cfl": ("football/cfl", "CFL"),
    "mlb": ("baseball/mlb", "MLB"),
    "mls": ("soccer/usa.1", "MLS"),
    "epl": ("soccer/eng.1", "Premier League"),
    "laliga": ("soccer/esp.1", "La Liga"),
    "bundesliga": ("soccer/ger.1", "Bundesliga"),
    "seriea": ("soccer/ita.1", "Serie A"),
    "ligue1": ("soccer/fra.1", "Ligue 1"),
    "ucl": ("soccer/uefa.champions", "Champions League"),
    "f1": ("racing/f1", "Formula 1"),
}
LEAGUE_ALIASES = {
    "premierleague": "epl", "englishpremierleague": "epl", "pl": "epl", "eng1": "epl",
    "championsleague": "ucl", "uefachampionsleague": "ucl",
    "formula1": "f1", "formulaone": "f1",
    "majorleaguesoccer": "mls", "usa1": "mls", "esp1": "laliga", "ger1": "bundesliga",
    "ita1": "seriea", "fra1": "ligue1",
}
# Bare symbols that mean the coin, not a stock with the same ticker
# (BTC and ETH are also US tickers). --stock overrides.
CRYPTO_HINTS = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LTC", "DOT", "AVAX", "LINK", "BNB",
                "TRX", "SHIB", "XLM", "BCH", "USDT", "USDC", "MATIC", "POL", "TON", "XMR", "ATOM", "PEPE"}


class WatchError(Exception):
    """A plain sentence for the owner or Atlas: what went wrong."""


# ------------------------------------------------------------ the store ----

def store_path() -> Path:
    return Path(os.getenv("ATLAS_WATCHES_FILE", "").strip() or DEFAULT_STORE)


def _empty() -> dict:
    return {"version": 1, "next_id": 1, "watches": []}


def _read() -> dict:
    path = store_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty()
    except (OSError, ValueError) as e:
        raise WatchError(f"can't read {path.name}: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("watches"), list):
        raise WatchError(f"{path.name} isn't a watch store")
    data.setdefault("next_id", 1)
    return data


def _write(data: dict) -> None:
    """Atomic: a temp file beside the store, then swapped in."""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(40):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # Windows: a reader has it open this instant
                if attempt == 39:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


@contextmanager
def _locked():
    """One writer at a time across the bot, Atlas's CLI runs and the app."""
    lock_path = store_path().with_name(store_path().name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+b")
    try:
        deadline = time.time() + 15
        while True:
            try:
                f.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() > deadline:
                    raise WatchError("the watch store is busy, try again")
                time.sleep(0.05)
        yield
    finally:
        try:
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        f.close()


def _mutate(fn):
    """Read, change, write under the lock; returns whatever fn returns."""
    with _locked():
        data = _read()
        out = fn(data)
        _write(data)
        return out


def _norm_id(wid) -> str:
    s = str(wid or "").strip().lower()
    return "w" + s if s.isdigit() else s


def _find(data: dict, wid) -> dict:
    wid = _norm_id(wid)
    for w in data["watches"]:
        if w["id"] == wid:
            return w
    raise WatchError(f"no watch {wid or '(blank)'}; `python watch.py list` shows the ids")


# -------------------------------------------------------------- helpers ----

def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def _parse_dt(s) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except (TypeError, ValueError):
        return None


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M%p").lstrip("0").lower()


def when(s) -> str:
    """'today 9:30pm', 'yesterday 7:05am', 'Nov 3, 6:00pm'; 'never' for None."""
    dt = _parse_dt(s)
    if not dt:
        return "never"
    today = datetime.now().date()
    if dt.date() == today:
        return f"today {_clock(dt)}"
    if dt.date() == today - timedelta(days=1):
        return f"yesterday {_clock(dt)}"
    if dt.date() == today + timedelta(days=1):
        return f"tomorrow {_clock(dt)}"
    year = f" {dt.year}" if dt.year != today.year else ""
    return f"{dt.strftime('%b')} {dt.day}{year}, {_clock(dt)}"


def _short(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:160]


def _squash(text: str) -> str:
    return " ".join((text or "").split())


def _cut(text: str, n: int) -> str:
    text = _squash(text)
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def _money(x: float) -> str:
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.2f}"
    return f"{x:.6g}"


def _num(x: float) -> str:
    return f"{x:,.10g}"


def _host(url: str) -> str:
    return urlparse(url).netloc or url


_http_cache: dict = {}


def _http(url: str, params: dict | None = None, ttl: int = 0) -> tuple[int, str]:
    """(status, body text). Network trouble raises WatchError; HTTP errors
    come back as the status so callers can say what they mean."""
    key = (url, tuple(sorted((params or {}).items())))
    hit = _http_cache.get(key)
    if ttl and hit and time.time() - hit[0] < ttl:
        return hit[1]
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT, stream=True,
                         cookies={"CONSENT": "YES+1"} if "youtube.com" in url else None)
        with r:
            body = b""
            for chunk in r.iter_content(65536):
                body += chunk
                if len(body) > MAX_BYTES:
                    break
            ctype = r.headers.get("content-type", "")
            m = re.search(r"charset=([\w.-]+)", ctype, re.I)
            text = None
            if m:
                try:
                    text = body.decode(m.group(1), "replace")
                except LookupError:
                    text = None
            if text is None:
                try:
                    text = body.decode("utf-8")
                except UnicodeDecodeError:
                    text = body.decode("cp1252", "replace")
            out = (r.status_code, text)
    except requests.RequestException as e:
        why = {"ConnectTimeout": "timed out", "ReadTimeout": "timed out",
               "SSLError": "bad certificate"}.get(type(e).__name__, "")
        if not why:
            why = "name not found" if "NameResolution" in str(e) or "getaddrinfo" in str(e) else type(e).__name__
        raise WatchError(f"couldn't reach {_host(url)} ({why})") from e
    if ttl and out[0] < 400:
        _http_cache[key] = (time.time(), out)
        if len(_http_cache) > 200:
            _http_cache.clear()
    return out


def _json(url: str, params: dict | None = None, ttl: int = 0):
    status, text = _http(url, params, ttl)
    if status == 429:
        raise WatchError(f"{_host(url)} is rate limiting right now (HTTP 429)")
    if status >= 400:
        raise WatchError(f"{_host(url)} answered HTTP {status}")
    try:
        return json.loads(text)
    except ValueError as e:
        raise WatchError(f"{_host(url)} sent something that isn't JSON") from e


def _outcome(state, value="", fires=None, next_in=None, note=""):
    return {"state": state, "value": value, "fires": fires or [], "next_in": next_in, "note": note}


# ---------------------------------------------------------------- email ----

def _prep_email(p: dict) -> tuple[dict, str]:
    match = _squash(str(p.get("match") or ""))
    field = (p.get("field") or "").strip().lower()
    if match.lower().startswith("subject:"):
        field, match = "subject", match[8:].strip()
    elif match.lower().startswith("from:"):
        field, match = "from", match[5:].strip()
    field = field or "from"
    match = match.strip("`\"'")
    if field not in ("from", "subject"):
        raise WatchError("an email watch matches on the sender (from) or the subject")
    if not match:
        raise WatchError("an email watch needs a sender or subject words to match")
    label = f"Mail from {match}" if field == "from" else f"Mail about {match}"
    return {"field": field, "match": match}, label


def email_rows() -> list[dict]:
    """Active email watches, for email_watch.py: [{id, field, needle, label, created}]."""
    try:
        watches = _read()["watches"]
    except WatchError:
        return []
    return [{"id": w["id"], "field": w["params"]["field"], "needle": w["params"]["match"].lower(),
             "label": w["label"], "created": w.get("created")}
            for w in watches if w["type"] == "email" and w["status"] == "active"]


def mark_fired(wid, note: str) -> None:
    """An email watch matched (called by email_watch.check)."""
    def fn(data):
        try:
            w = _find(data, wid)
        except WatchError:
            return
        now = _iso(_now())
        w["last_fired"] = w["last_checked"] = now
        w["last_note"] = _cut(note, 300)
        w["last_error"] = ""
    _mutate(fn)


def note_email_sweep(error: str) -> None:
    """Stamp every active email watch with the sweep's time and result."""
    def fn(data):
        now = _iso(_now())
        for w in data["watches"]:
            if w["type"] == "email" and w["status"] == "active":
                w["last_checked"] = now
                w["last_error"] = error
                w["error_count"] = (w.get("error_count", 0) + 1) if error else 0
    if any(True for _ in email_rows()):
        _mutate(fn)


def _peek_email(w: dict) -> dict:
    import email_watch  # beside this file; needs the Gmail creds in Atlas\.env
    hits, err = email_watch.check(dry_run=True)
    if err == "nocreds":
        raise WatchError("Gmail isn't set up (no app password in Atlas\\.env)")
    if err:
        raise WatchError(f"Gmail check failed: {err}")
    mine = [h for h in hits if h.get("id") == w["id"]]
    if not mine:
        return _outcome(w.get("state", {}), "no new matching mail")
    lines = [f"from {h['from']}: {h['subject']}" for h in mine]
    return _outcome(w.get("state", {}), f"{len(mine)} new matching: " + "; ".join(lines),
                    fires=[f"Matching mail {line}" for line in lines])


# ---------------------------------------------------------------- price ----

_CONDITIONS = {"above": "above", "over": "above", ">": "above", ">=": "above",
               "below": "below", "under": "below", "<": "below", "<=": "below",
               "up": "up", "rises": "up", "gains": "up",
               "down": "down", "drops": "down", "falls": "down",
               "move": "move", "moves": "move", "swing": "move", "swings": "move"}


def _yahoo(symbol: str, ttl: int = 0) -> dict | None:
    for host in ("query1", "query2"):
        try:
            status, text = _http(f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}",
                                 {"range": "1d", "interval": "1d"}, ttl)
        except WatchError:
            if host == "query2":
                raise
            continue
        if status == 404:
            return None
        if status >= 400:
            if host == "query2":
                raise WatchError(f"Yahoo Finance answered HTTP {status}")
            continue
        try:
            res = (json.loads(text).get("chart") or {}).get("result")
        except ValueError:
            res = None
        if not res:
            return None
        return res[0].get("meta") or {}
    return None


def _stock_quote(symbol: str, ttl: int = 0) -> dict:
    meta = _yahoo(symbol, ttl)
    if meta is None or meta.get("regularMarketPrice") is None:
        raise WatchError(f"Yahoo Finance has no price for {symbol}")
    price = float(meta["regularMarketPrice"])
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    change = (price - prev) / prev * 100 if prev else None
    offset = (meta.get("currentTradingPeriod") or {}).get("regular", {}).get("gmtoffset", 0)
    stamp = meta.get("regularMarketTime") or time.time()
    day = datetime.fromtimestamp(stamp + offset, timezone.utc).strftime("%Y-%m-%d")
    return {"price": price, "change": change, "currency": meta.get("currency") or "",
            "name": meta.get("shortName") or meta.get("longName") or symbol, "day": day,
            "period": meta.get("currentTradingPeriod") or {}}


def _coin_lookup(symbol: str) -> tuple[str, str] | None:
    data = _json("https://api.coingecko.com/api/v3/search", {"query": symbol})
    coins = data.get("coins") or []
    want = symbol.lower()
    exact = [c for c in coins if (c.get("symbol") or "").lower() == want]
    exact.sort(key=lambda c: c.get("market_cap_rank") or 10**9)
    pick = exact[0] if exact else next((c for c in coins if c.get("id") == want), None)
    return (pick["id"], pick.get("name") or pick["id"]) if pick else None


def _coin_quote(coin_id: str, currency: str, ttl: int = 0) -> dict:
    data = _json("https://api.coingecko.com/api/v3/simple/price",
                 {"ids": coin_id, "vs_currencies": currency, "include_24hr_change": "true"}, ttl)
    row = data.get(coin_id) or {}
    if row.get(currency) is None:
        raise WatchError(f"CoinGecko has no {currency.upper()} price for {coin_id}")
    return {"price": float(row[currency]), "change": row.get(f"{currency}_24h_change"),
            "currency": currency.upper(), "day": datetime.now().strftime("%Y-%m-%d")}


def _prep_price(p: dict) -> tuple[dict, str]:
    symbol = re.sub(r"\s+", "", str(p.get("symbol") or "")).upper().lstrip("$")
    if not symbol:
        raise WatchError("a price watch needs a symbol, like AAPL or BTC")
    cond = _CONDITIONS.get(str(p.get("condition") or "").strip().lower())
    if not cond:
        raise WatchError("the condition is above, below, up, down or move")
    raw = str(p.get("value") if p.get("value") is not None else "").strip()
    try:
        value = float(raw.replace(",", "").replace("$", "").rstrip("%"))
    except ValueError:
        raise WatchError(f"{raw or 'nothing'} isn't a number") from None
    if value <= 0:
        raise WatchError("the number has to be above zero")
    kind = (p.get("kind") or "auto").strip().lower()
    if kind not in ("auto", "stock", "crypto"):
        raise WatchError("kind is stock, crypto or auto")
    currency = (p.get("currency") or "usd").strip().lower()
    order = {"stock": ["stock"], "crypto": ["crypto"]}.get(
        kind, ["crypto", "stock"] if symbol in CRYPTO_HINTS else ["stock", "crypto"])
    out = None
    for k in order:
        if k == "stock":
            meta = _yahoo(symbol)
            if meta and meta.get("regularMarketPrice") is not None:
                out = {"symbol": symbol, "kind": "stock",
                       "name": meta.get("shortName") or meta.get("longName") or symbol,
                       "currency": meta.get("currency") or ""}
        else:
            found = _coin_lookup(symbol.split("-")[0])
            if found:
                out = {"symbol": symbol.split("-")[0], "kind": "crypto", "coin_id": found[0],
                       "name": found[1], "currency": currency.upper()}
        if out:
            break
    if not out:
        where = {"stock": "on Yahoo Finance", "crypto": "on CoinGecko"}.get(kind, "as a stock or a coin")
        raise WatchError(f"couldn't find {symbol} {where}")
    out.update(condition=cond, value=value)
    return out, _price_label(out)


def _price_label(p: dict) -> str:
    v = p["value"]
    return {"above": f"{p['symbol']} above {_num(v)}", "below": f"{p['symbol']} below {_num(v)}",
            "up": f"{p['symbol']} up {_num(v)}% in a day", "down": f"{p['symbol']} down {_num(v)}% in a day",
            "move": f"{p['symbol']} moves {_num(v)}% in a day"}[p["condition"]]


def _run_price(w: dict, state: dict, prime: bool) -> dict:
    p = w["params"]
    if p["kind"] == "crypto":
        q = _coin_quote(p["coin_id"], p["currency"].lower(), ttl=60)
        next_in = INTERVAL["price"]
    else:
        q = _stock_quote(p["symbol"], ttl=60)
        now_ts = time.time()
        reg, pre = q["period"].get("regular") or {}, q["period"].get("pre") or {}
        if reg.get("start", 0) <= now_ts < reg.get("end", 0):
            next_in = INTERVAL["price"]  # market open: every 5 minutes
        elif pre.get("start", 0) <= now_ts < reg.get("start", 0):
            next_in = max(60, min(1800, reg["start"] - now_ts + 30))  # wake for the open
        else:
            next_in = 1800  # closed: prices barely move
    price, change, cur = q["price"], q["change"], q["currency"]
    value = f"{_money(price)} {cur}".strip()
    if change is not None:
        value += f", {change:+.2f}% {'24h' if p['kind'] == 'crypto' else 'today'}"
    cond, n = p["condition"], p["value"]
    who = p["symbol"] if p.get("name", "").upper() == p["symbol"] else f"{p['symbol']} ({p.get('name')})"
    fires, note = [], ""
    if cond in ("above", "below"):
        hit = price >= n if cond == "above" else price <= n
        if prime or "armed" not in state:
            state["armed"] = not hit
            if hit:
                note = (f"It's already {cond} {_num(n)}, so it fires after it goes back "
                        f"{'under' if cond == 'above' else 'over'} and crosses again.")
        elif hit and state["armed"]:
            fires.append(f"{who} is {value}: crossed {cond} {_num(n)}.")
            state["armed"] = False
        elif not hit:
            state["armed"] = True
    else:
        hit = change is not None and (abs(change) >= n if cond == "move"
                                      else change >= n if cond == "up" else change <= -n)
        if prime or "fired_day" not in state:
            state["fired_day"] = q["day"] if hit else None
            if hit:
                note = "It has already moved that much today, so the next alert is for another day."
        elif hit and state.get("fired_day") != q["day"]:
            span = "in the last 24 hours" if p["kind"] == "crypto" else "today"
            fires.append(f"{who} is {value}: moved {change:+.2f}% {span} (watching for {_num(n)}%).")
            state["fired_day"] = q["day"]
    return _outcome(state, value, fires, next_in, note)


# ------------------------------------------------------------------ web ----

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
         "source", "track", "wbr"}
_NO_TEXT = {"script", "style", "noscript", "template", "svg", "head", "iframe", "object"}


class _Node:
    __slots__ = ("tag", "attrs", "kids", "parent")

    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.kids, self.parent = tag, attrs, [], parent


class _Tree(HTMLParser):
    """Just enough of a DOM for CSS selectors and visible text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.cur = self.root

    def _node(self, tag, attrs):
        node = _Node(tag, {k.lower(): (v or "") for k, v in attrs}, self.cur)
        self.cur.kids.append(node)
        return node

    def handle_starttag(self, tag, attrs):
        node = self._node(tag, attrs)
        if tag not in _VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self._node(tag, attrs)

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.kids.append(data)


def _walk(root):
    stack = [root]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(k for k in reversed(n.kids) if not isinstance(k, str))


def _text_of(node) -> str:
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        if isinstance(n, str):
            out.append(n)
            continue
        if n.tag in _NO_TEXT:
            continue
        out.append(" ")
        stack.extend(reversed(n.kids))
    return _squash(" ".join(out))


_COMPOUND = re.compile(
    r"""(?P<tag>^[a-zA-Z][\w-]*|^\*)"""
    r"""|\#(?P<id>[\w-]+)"""
    r"""|\.(?P<cls>[\w-]+)"""
    r"""|\[\s*(?P<attr>[\w:-]+)\s*(?:(?P<op>[~^$*|]?=)\s*"""
    r"""(?:"(?P<v1>[^"]*)"|'(?P<v2>[^']*)'|(?P<v3>[^\]\s]+)))?\s*\]""")


def _compound(tok: str) -> dict:
    c = {"tag": None, "id": None, "cls": [], "attrs": []}
    pos = 0
    while pos < len(tok):
        m = _COMPOUND.match(tok, pos)
        if not m or m.end() == pos:
            raise WatchError(f"can't use the CSS selector part {tok[pos:]!r} (tags, #id, .class and [attr=value] work)")
        if m.group("tag"):
            c["tag"] = None if m.group("tag") == "*" else m.group("tag").lower()
        elif m.group("id"):
            c["id"] = m.group("id")
        elif m.group("cls"):
            c["cls"].append(m.group("cls"))
        else:
            val = next((v for v in (m.group("v1"), m.group("v2"), m.group("v3")) if v is not None), None)
            c["attrs"].append((m.group("attr").lower(), m.group("op"), val))
        pos = m.end()
    return c


def _split_selector(part: str) -> list[str]:
    toks, cur, depth, quote = [], "", 0, ""
    for ch in part:
        if quote:
            cur += ch
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote, cur = ch, cur + ch
        elif ch == "[":
            depth, cur = depth + 1, cur + ch
        elif ch == "]":
            depth, cur = max(0, depth - 1), cur + ch
        elif depth == 0 and (ch.isspace() or ch == ">"):
            if cur:
                toks.append(cur)
                cur = ""
            if ch == ">":
                toks.append(">")
        else:
            cur += ch
    if cur:
        toks.append(cur)
    return toks


def parse_selector(sel: str) -> list[list[tuple[str, dict]]]:
    groups = []
    for part in sel.split(","):
        steps, comb = [], " "
        for tok in _split_selector(part.strip()):
            if tok == ">":
                comb = ">"
                continue
            steps.append((comb, _compound(tok)))
            comb = " "
        if steps:
            groups.append(steps)
    if not groups:
        raise WatchError("the CSS selector is empty")
    return groups


def _cmatch(n, c) -> bool:
    if n.tag == "#root" or (c["tag"] and n.tag != c["tag"]):
        return False
    if c["id"] and n.attrs.get("id") != c["id"]:
        return False
    if c["cls"]:
        have = n.attrs.get("class", "").split()
        if not all(x in have for x in c["cls"]):
            return False
    for name, op, val in c["attrs"]:
        if name not in n.attrs:
            return False
        got = n.attrs[name]
        if op is None:
            continue
        if not ((op == "=" and got == val) or (op == "~=" and val in got.split())
                or (op == "^=" and got.startswith(val)) or (op == "$=" and got.endswith(val))
                or (op == "*=" and val in got) or (op == "|=" and (got == val or got.startswith(val + "-")))):
            return False
    return True


def _match_up(n, steps, i) -> bool:
    if i == 0:
        return True
    comb, target = steps[i][0], steps[i - 1][1]
    p = n.parent
    if comb == ">":
        return p is not None and _cmatch(p, target) and _match_up(p, steps, i - 1)
    while p is not None:
        if _cmatch(p, target) and _match_up(p, steps, i - 1):
            return True
        p = p.parent
    return False


def page_region(html_text: str, selector: str = "") -> tuple[str, int]:
    """(visible text, how many elements the selector matched). No selector:
    the whole body's text, count 1."""
    tree = _Tree()
    try:
        tree.feed(html_text)
        tree.close()
    except Exception:  # noqa: BLE001 - malformed pages: use what was parsed
        pass
    if not selector:
        body = next((n for n in _walk(tree.root) if n.tag == "body"), tree.root)
        return _text_of(body), 1
    groups = parse_selector(selector)
    hits = [n for n in _walk(tree.root) if any(_cmatch(n, g[-1][1]) and _match_up(n, g, len(g) - 1) for g in groups)]
    return _squash(" ".join(_text_of(n) for n in hits)), len(hits)


def _prep_web(p: dict) -> tuple[dict, str]:
    url = str(p.get("url") or "").strip()
    if not url:
        raise WatchError("a web watch needs a URL")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    if not urlparse(url).netloc:
        raise WatchError(f"{url} isn't a web address")
    selector = _squash(str(p.get("selector") or ""))
    if selector:
        parse_selector(selector)
    text = _squash(str(p.get("text") or ""))
    mode = (p.get("mode") or ("appears" if text else "change")).strip().lower()
    if mode not in ("change", "appears", "disappears"):
        raise WatchError("mode is change, appears or disappears")
    if mode != "change" and not text:
        raise WatchError(f"'{mode}' needs the text to look for (--text)")
    out = {"url": url, "selector": selector, "text": text, "mode": mode}
    where = _host(url) + (f" ({selector})" if selector else "")
    label = {"change": f"{where} changes", "appears": f'"{text}" shows up on {where}',
             "disappears": f'"{text}" goes away on {where}'}[mode]
    return out, label


def _diff_note(old: str, new: str) -> str:
    a, b = old.split(), new.split()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    added, gone = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op in ("replace", "insert"):
            added.append(" ".join(b[j1:j2]))
        if op in ("replace", "delete"):
            gone.append(" ".join(a[i1:i2]))
    parts = []
    if added:
        parts.append("New: " + _cut(" | ".join(added), 400))
    if gone:
        parts.append("Gone: " + _cut(" | ".join(gone), 200))
    return " ".join(parts) or "The text changed."


def _run_web(w: dict, state: dict, prime: bool) -> dict:
    p = w["params"]
    status, body = _http(p["url"])
    if status >= 400:
        raise WatchError(f"{_host(p['url'])} answered HTTP {status}")
    text, count = page_region(body, p["selector"])
    missing = bool(p["selector"]) and count == 0
    fires, note = [], ""
    if p["mode"] == "change":
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if prime or "hash" not in state:
            if missing and prime:
                raise WatchError(f"the selector {p['selector']} matches nothing on that page")
        elif digest != state["hash"]:
            what = "The part you watch is gone from the page." if missing else _diff_note(state.get("text", ""), text)
            fires.append(f"{p['url']} changed. {what}")
        state["hash"], state["text"] = digest, text[:KEEP_TEXT]
        value = "the selector matches nothing right now" if missing else _cut(text, 140) or "(no text)"
    else:
        present = p["text"].lower() in text.lower()
        if prime or "present" not in state:
            if present == (p["mode"] == "appears"):
                note = (f"It's already {'on' if present else 'off'} the page, so it fires if that flips "
                        "and then flips back.")
        elif present != state["present"] and present == (p["mode"] == "appears"):
            fires.append(f'"{p["text"]}" {"is now on" if present else "is gone from"} {p["url"]}')
        state["present"] = present
        value = f'"{_cut(p["text"], 60)}" is {"on" if present else "not on"} the page'
        if missing:
            value += " (the selector matches nothing right now)"
    return _outcome(state, value, fires, INTERVAL["web"], note)


# -------------------------------------------------------------- youtube ----

_ATOM = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


def _resolve_channel(raw: str) -> tuple[str, str]:
    """(channel id, channel name) from a URL, @handle or UC id."""
    raw = raw.strip()
    m = re.search(r"(UC[\w-]{22})", raw)
    if m and ("/channel/" in raw or re.fullmatch(r"UC[\w-]{22}", raw)):
        cid = m.group(1)
        entries, name = _feed(cid)
        return cid, name
    if raw.startswith("@"):
        url = f"https://www.youtube.com/{raw}"
    elif re.match(r"^(https?://)?(www\.|m\.)?youtube\.com/", raw, re.I):
        url = raw if raw.lower().startswith("http") else "https://" + raw
    elif re.fullmatch(r"[\w.-]{3,}", raw):
        url = f"https://www.youtube.com/@{raw}"
    else:
        raise WatchError(f"{raw} isn't a YouTube channel link, @handle or channel id")
    status, page = _http(url)
    if status == 404:
        raise WatchError(f"YouTube has no channel at {url}")
    if status >= 400:
        raise WatchError(f"YouTube answered HTTP {status}")
    m = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', page)
         or re.search(r'"externalId":"(UC[\w-]{22})"', page)
         or re.search(r'"channelId":"(UC[\w-]{22})"', page))
    if not m:
        raise WatchError(f"couldn't find the channel id on {url}")
    cid = m.group(1)
    entries, name = _feed(cid)
    return cid, name


def _feed(cid: str) -> tuple[list[dict], str]:
    status, text = _http("https://www.youtube.com/feeds/videos.xml", {"channel_id": cid})
    if status == 404:
        raise WatchError(f"YouTube has no feed for channel {cid}")
    if status >= 400:
        raise WatchError(f"YouTube's feed answered HTTP {status}")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise WatchError("YouTube's feed came back unreadable") from e
    name = root.findtext("a:title", "", _ATOM) or cid
    entries = []
    for e in root.findall("a:entry", _ATOM):
        vid = e.findtext("yt:videoId", "", _ATOM)
        if vid:
            entries.append({"id": vid, "title": e.findtext("a:title", "", _ATOM),
                            "published": e.findtext("a:published", "", _ATOM)})
    return entries, name


def _prep_youtube(p: dict) -> tuple[dict, str]:
    raw = str(p.get("channel") or "").strip()
    if not raw:
        raise WatchError("a YouTube watch needs a channel link, @handle or channel id")
    cid, name = _resolve_channel(raw)
    return {"channel": raw, "channel_id": cid, "channel_name": name}, f"New upload from {name}"


def _run_youtube(w: dict, state: dict, prime: bool) -> dict:
    p = w["params"]
    entries, _ = _feed(p["channel_id"])
    entries.sort(key=lambda e: e["published"], reverse=True)
    ids = [e["id"] for e in entries]
    newest = entries[0]["published"] if entries else ""
    value = f'latest: "{_cut(entries[0]["title"], 90)}"' if entries else "no uploads yet"
    fires = []
    if prime or "seen" not in state:
        state["seen"], state["newest"] = ids[:60], newest
    else:
        seen = set(state["seen"])
        fresh = [e for e in entries if e["id"] not in seen and e["published"] > (state.get("newest") or "")]
        for e in reversed(fresh):
            fires.append(f'{p["channel_name"]} uploaded "{e["title"]}": https://www.youtube.com/watch?v={e["id"]}')
        state["seen"] = (ids + [i for i in state["seen"] if i not in ids])[:60]
        state["newest"] = max(newest, state.get("newest") or "")
    return _outcome(state, value, fires, INTERVAL["youtube"])


# ----------------------------------------------------------------- team ----

def league_key(raw: str) -> str | None:
    k = re.sub(r"[\s._-]+", "", str(raw or "").lower())
    k = LEAGUE_ALIASES.get(k, k)
    return k if k in LEAGUES else None


def leagues() -> list[dict]:
    return [{"key": k, "name": v[1], "individual": k == "f1"} for k, v in LEAGUES.items()]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _prep_team(p: dict) -> tuple[dict, str]:
    key = league_key(p.get("league"))
    if not key:
        raise WatchError(f"don't know the league {p.get('league') or '(blank)'}; try "
                         + ", ".join(LEAGUES))
    path, lname = LEAGUES[key]
    want = _squash(str(p.get("team") or ""))
    start = bool(p.get("start"))
    if key == "f1":
        label = f"F1: {want}" if want else "F1 races"
        return {"league": key, "team": want, "start": start}, label
    if not want:
        raise WatchError(f"which {lname} team?")
    data = _json(ESPN + path + "/teams", ttl=3600)
    try:
        teams = [t["team"] for t in data["sports"][0]["leagues"][0]["teams"]]
    except (KeyError, IndexError, TypeError):
        raise WatchError(f"ESPN didn't send a {lname} team list") from None
    q = _norm(want)
    names = lambda t: [t.get(k) for k in ("displayName", "shortDisplayName", "name", "nickname",
                                          "location", "abbreviation", "slug")]
    pick = [t for t in teams if q in {_norm(n) for n in names(t) if n}]
    if not pick:
        pick = [t for t in teams if q and q in _norm(t.get("displayName"))]
    if not pick:
        raise WatchError(f"no {lname} team matches {want}")
    if len(pick) > 1:
        raise WatchError(f"{want} could be " + " or ".join(t["displayName"] for t in pick[:6]) + "; say which")
    t = pick[0]
    return ({"league": key, "team": t["displayName"], "team_id": str(t["id"]),
             "abbr": t.get("abbreviation", ""), "short": t.get("shortDisplayName") or t.get("name") or t["displayName"],
             "start": start}, f"{t['displayName']} games")


def _espn_dt(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _et_days() -> list[str]:
    """ESPN files games under US Eastern dates: yesterday's covers a late game
    that ends after midnight."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except Exception:  # noqa: BLE001 - no tz database: a fixed offset is close enough
        tz = timezone(timedelta(hours=-5))
    today = datetime.now(tz).date()
    return [(today - timedelta(days=1)).strftime("%Y%m%d"), today.strftime("%Y%m%d")]


def _game_line(comp: dict) -> tuple[str, list]:
    """'Bruins 4, Red Wings 2' (away first) and the competitors."""
    cs = sorted(comp.get("competitors") or [], key=lambda c: c.get("homeAway") != "away")
    score = lambda c: c.get("score", {}).get("displayValue") if isinstance(c.get("score"), dict) else c.get("score")
    return ", ".join(f"{c['team'].get('shortDisplayName') or c['team'].get('displayName')} {score(c) or 0}"
                     for c in cs), cs


def _team_next(starts: list, live: bool) -> int:
    if live:
        return 120
    if starts:
        secs = (min(starts) - datetime.now(timezone.utc)).total_seconds()
        if secs <= 15 * 60:
            return 120  # in the window: every 2 minutes until it's on
        return int(min(3600, secs - 10 * 60))
    return INTERVAL["team"]


def _run_team(w: dict, state: dict, prime: bool) -> dict:
    p = w["params"]
    if p["league"] == "f1":
        return _run_f1(w, state, prime)
    path = LEAGUES[p["league"]][0]
    events = []
    for day in _et_days():
        events += _json(ESPN + path + "/scoreboard", {"dates": day}, ttl=60).get("events") or []
    games = state.get("games") or {}
    primed = "games" in state and not prime
    fires, starts, live, view, value = [], [], False, {}, ""
    for e in events:
        comp = (e.get("competitions") or [{}])[0]
        if p["team_id"] not in [str(c.get("team", {}).get("id")) for c in comp.get("competitors") or []]:
            continue
        eid = str(e.get("id"))
        g = dict(games.get(eid) or {})
        st = (comp.get("status") or e.get("status") or {}).get("type") or {}
        phase, line = st.get("state"), _game_line(comp)
        score, cs = line
        title = e.get("shortName") or e.get("name") or "the game"
        if phase == "pre":
            dt = _espn_dt(comp.get("date") or e.get("date"))
            if dt:
                starts.append(dt)
            value = value or f"next: {title}, {st.get('shortDetail') or ''}".rstrip(", ")
        elif phase == "in":
            live = True
            value = f"{score}, {st.get('shortDetail') or 'live'}"
            if p.get("start") and not g.get("start") and primed:
                fires.append(f"{e.get('name') or title} just started.")
            g["start"] = True
        elif phase == "post":
            if not g.get("final"):
                if not st.get("completed"):
                    note = f"{e.get('name') or title}: {st.get('description') or 'not played'}."
                else:
                    mine = next((c for c in cs if str(c["team"].get("id")) == p["team_id"]), {})
                    other = next((c for c in cs if c is not mine), {})
                    if mine.get("winner"):
                        verdict = f"{p['short']} won."
                    elif other.get("winner"):
                        verdict = f"{p['short']} lost."
                    else:
                        verdict = "It ended level."
                    detail = st.get("shortDetail") or "Final"
                    note = f"{detail}: {score}. {verdict}"
                if primed:
                    fires.append(note)
            g["final"] = g["start"] = True
            if not live:
                value = f"{st.get('shortDetail') or 'Final'}: {score}"
        view[eid] = g
    state["games"] = view
    if not value:
        value = "no game today"
    return _outcome(state, value, fires, _team_next(starts, live))


def _run_f1(w: dict, state: dict, prime: bool) -> dict:
    p = w["params"]
    data = _json(ESPN + "racing/f1/scoreboard", ttl=60)
    games = state.get("games") or {}
    primed = "games" in state and not prime
    fires, starts, live, view, value = [], [], False, dict(games), ""
    for e in data.get("events") or []:
        for comp in e.get("competitions") or []:
            if (comp.get("type") or {}).get("abbreviation") != "Race":
                continue
            key = f"{e.get('id')}-{comp.get('id')}"
            g = dict(games.get(key) or {})
            st = (comp.get("status") or {}).get("type") or {}
            phase, name = st.get("state"), e.get("name") or "the race"
            if phase == "pre":
                dt = _espn_dt(comp.get("date"))
                if dt:
                    starts.append(dt)
                value = f"next: {name}, {st.get('shortDetail') or ''}".rstrip(", ")
            elif phase == "in":
                live = True
                value = f"{name} is on"
                if p.get("start") and not g.get("start") and primed:
                    fires.append(f"{name} just started.")
                g["start"] = True
            elif phase == "post":
                field = sorted(comp.get("competitors") or [], key=lambda c: c.get("order") or 99)
                driver = lambda c: (c.get("athlete") or {}).get("displayName") or "?"
                winner = driver(field[0]) if field else "unknown"
                note = f"{name} finished. Winner: {winner}."
                if p.get("team"):
                    q = _norm(p["team"])
                    mine = next((c for c in field if q and any(q in _norm(v) for v in (c.get("athlete") or {}).values()
                                                               if isinstance(v, str))), None)
                    note += f" {driver(mine)}: P{mine.get('order')}." if mine else f" No result for {p['team']}."
                if not st.get("completed"):
                    note = f"{name}: {st.get('description') or 'not run'}."
                if not g.get("final") and primed:
                    fires.append(note)
                g["final"] = g["start"] = True
                value = value or note
            view[key] = g
    state["games"] = dict(list(view.items())[-20:])
    return _outcome(state, value or "no race on ESPN's schedule right now", fires, _team_next(starts, live))


# ---------------------------------------------------------- the engine ----

PREP = {"email": _prep_email, "price": _prep_price, "web": _prep_web,
        "youtube": _prep_youtube, "team": _prep_team}
RUN = {"price": _run_price, "web": _run_web, "youtube": _run_youtube, "team": _run_team}


def describe(w: dict) -> str:
    """What it watches, in a few plain words."""
    p, t = w["params"], w["type"]
    if t == "email":
        return f'{"Sender" if p["field"] == "from" else "Subject"} contains "{p["match"]}"'
    if t == "price":
        who = p["symbol"] + (f" ({p['name']})" if p.get("name") and p["name"].upper() != p["symbol"] else "")
        cond = {"above": f"above {_num(p['value'])} {p.get('currency', '')}",
                "below": f"below {_num(p['value'])} {p.get('currency', '')}",
                "up": f"up {_num(p['value'])}% in a day", "down": f"down {_num(p['value'])}% in a day",
                "move": f"moves {_num(p['value'])}% in a day"}[p["condition"]]
        return f"{who} {cond.strip()}, {p['kind']}"
    if t == "web":
        base = p["url"] + (f" [{p['selector']}]" if p["selector"] else "")
        return base + {"change": ", any change", "appears": f', when "{p["text"]}" shows up',
                       "disappears": f', when "{p["text"]}" goes away'}[p["mode"]]
    if t == "youtube":
        return f"New uploads on {p['channel_name']}"
    if t == "team":
        lname = LEAGUES.get(p["league"], ("", p["league"]))[1]
        what = "race results" if p["league"] == "f1" else "final scores"
        who = p["team"] or "every race"
        return f"{who} ({lname}): {what}" + (", starts" if p.get("start") else "")
    return t


def public(w: dict) -> dict:
    """A watch as the app shows it: everything but the checker's private state."""
    out = {k: v for k, v in w.items() if k != "state"}
    out["summary"] = describe(w)
    return out


def load(include_all: bool = True) -> list[dict]:
    watches = _read()["watches"]
    return [copy.deepcopy(w) for w in watches if include_all or w["status"] == "active"]


def _run_one(w: dict, prime: bool) -> dict:
    try:
        if w["type"] == "email":
            if prime:
                return _outcome({}, "")
            return _peek_email(w)
        return RUN[w["type"]](w, copy.deepcopy(w.get("state") or {}), prime)
    except WatchError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001 - a checker bug must never take the bot down
        return {"error": f"check crashed ({_short(e)})"}


def add(wtype: str, params: dict | None = None) -> tuple[dict, str]:
    """Validate, resolve, take a baseline, save. Returns (watch, message).
    Raises WatchError (nothing saved) when it can't be set."""
    wtype = str(wtype or "").strip().lower()
    if wtype not in PREP:
        raise WatchError(f"unknown watch type {wtype or '(blank)'}; use " + ", ".join(TYPES))
    params = dict(params or {})
    label = _squash(str(params.pop("label", "") or ""))
    clean, default_label = PREP[wtype](params)
    for w in _read()["watches"]:
        if w["type"] == wtype and w["status"] == "active" and _same(w["params"], clean):
            return copy.deepcopy(w), f"OK: already watching that as {w['id']} ({w['label']})."
    w = {"id": "", "type": wtype, "label": label or default_label, "params": clean,
         "created": _iso(_now()), "status": "active", "last_checked": None, "last_fired": None,
         "last_value": "", "last_note": "", "last_error": "", "error_count": 0,
         "next_check": None, "state": {}}
    res = _run_one(w, prime=True)
    if "error" in res:
        raise WatchError(res["error"])
    w["state"], w["last_value"] = res["state"], res["value"]
    if wtype != "email":
        w["last_checked"] = w["created"]
        w["next_check"] = _iso(_now() + timedelta(seconds=res["next_in"] or INTERVAL[wtype]))

    def fn(data):
        for x in data["watches"]:  # the same add racing in from somewhere else
            if x["type"] == wtype and x["status"] == "active" and _same(x["params"], clean):
                return copy.deepcopy(x)
        w["id"] = f"w{data['next_id']}"
        data["next_id"] += 1
        data["watches"].append(w)
        return None
    twin = _mutate(fn)
    if twin:
        return twin, f"OK: already watching that as {twin['id']} ({twin['label']})."
    msg = f"OK: watch {w['id']} set ({wtype}): {w['label']}. {describe(w)}."
    if res["value"]:
        msg += f" Now: {res['value'].rstrip('.')}."
    if res.get("note"):
        msg += " " + res["note"]
    return copy.deepcopy(w), msg


def _same(a: dict, b: dict) -> bool:
    keys = {"email": ("field", "match"), "price": ("symbol", "kind", "condition", "value", "currency"),
            "web": ("url", "selector", "text", "mode"), "youtube": ("channel_id",),
            "team": ("league", "team", "team_id", "start")}
    for ks in keys.values():
        if all(k in a and k in b for k in ks):
            return all(str(a.get(k)).lower() == str(b.get(k)).lower() for k in ks)
    return False


def set_status(wid, status: str) -> tuple[dict, str]:
    status = str(status or "").strip().lower()
    if status not in STATUSES:
        raise WatchError("status is active, paused or done")

    def fn(data):
        w = _find(data, wid)
        w["status"] = status
        if status == "active":
            w["next_check"] = None  # check soon after a resume
            w["error_count"] = 0
        return copy.deepcopy(w)
    w = _mutate(fn)
    word = {"active": "resumed", "paused": "paused", "done": "marked done"}[status]
    return w, f"OK: watch {w['id']} ({w['label']}) {word}."


def remove(wid) -> str:
    def fn(data):
        w = _find(data, wid)
        data["watches"].remove(w)
        return w
    w = _mutate(fn)
    return f"OK: watch {w['id']} ({w['label']}) removed."


def _commit(wid: str, res: dict, peek: bool) -> list[dict]:
    """Save one check's result onto the watch, re-read under the lock so a
    change made meanwhile (an add, a pause) is never lost. Returns the fires
    to report, which a peek never does."""
    def fn(data):
        try:
            w = _find(data, wid)
        except WatchError:
            return []  # removed while it was being checked
        now = _now()
        w["last_checked"] = _iso(now)
        if "error" in res:
            w["last_error"] = res["error"]
            if peek:
                return []
            w["error_count"] = w.get("error_count", 0) + 1
            w["next_check"] = _iso(now + timedelta(seconds=ERROR_RETRY.get(w["type"], 900)))
            if w["error_count"] == FAIL_ALERT and w["status"] == "active":
                return [_fire(w, f"This watch has failed its last {FAIL_ALERT} checks in a row "
                                 f"(latest: {res['error']}), so it may be broken.", error=True)]
            return []
        w["last_error"], w["error_count"], w["last_value"] = "", 0, res["value"]
        if peek:
            return []
        w["state"] = res["state"]
        w["next_check"] = _iso(now + timedelta(seconds=res["next_in"] or INTERVAL.get(w["type"], 900)))
        if not res["fires"] or w["status"] != "active":
            return []
        w["last_fired"] = _iso(now)
        note = "\n".join(res["fires"])
        w["last_note"] = note if len(note) <= 500 else note[:497].rstrip() + "..."
        return [_fire(w, "\n".join(res["fires"]))]
    return _mutate(fn)


def _fire(w: dict, note: str, error: bool = False) -> dict:
    return {"id": w["id"], "type": w["type"], "label": w["label"], "summary": describe(w),
            "note": note, "error": error, "outside": w["type"] in ("web", "youtube")}


def run_due(now: datetime | None = None) -> list[dict]:
    """The bot's tick: check every active non-email watch that's due, save
    the results, and return what fired: [{id, type, label, summary, note,
    error, outside}]. outside = the note quotes a stranger's page or title."""
    now = now or _now()
    fires = []
    for w in _read()["watches"]:
        if w["status"] != "active" or w["type"] == "email":
            continue
        due = _parse_dt(w.get("next_check"))
        if due and due > now:
            continue
        fires += _commit(w["id"], _run_one(w, prime=False), peek=False)
    return fires


def check(wid=None) -> list[tuple[dict, str]]:
    """Peek now: [(watch, plain line)]. No id = every active watch."""
    data = _read()
    targets = [_find(data, wid)] if wid else [w for w in data["watches"] if w["status"] == "active"]
    out = []
    for w in targets:
        res = _run_one(w, prime=False)
        _commit(w["id"], res, peek=True)
        if "error" in res:
            line = f"{w['id']} {w['label']}: CHECK FAILED: {res['error']}"
        else:
            line = f"{w['id']} {w['label']}: {(res['value'] or 'checked').rstrip('.')}"
            if res["fires"]:
                line += ". Would fire now (the bot sends it on its next check): " + " ".join(res["fires"])
            else:
                line += ". Nothing to report."
        fresh = next((x for x in _read()["watches"] if x["id"] == w["id"]), w)
        out.append((copy.deepcopy(fresh), line))
    return out


# ------------------------------------------------------------------ CLI ----

USAGE = __doc__[__doc__.index("CLI ("):].strip()


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(f"ERROR: {message}. Run `python watch.py -h` for usage.")
        sys.exit(1)


def _cli_params(wtype: str, rest: list[str], a) -> dict:
    p = {"label": a.label}
    if wtype == "email":
        p["match"] = " ".join(rest)
    elif wtype == "price":
        toks = " ".join(rest).replace("%", " ").split()
        if len(toks) < 3:
            raise WatchError("price needs SYMBOL CONDITION NUMBER, like: add price AAPL above 250")
        p.update(symbol=toks[0], condition=toks[1], value="".join(toks[2:]),
                 kind="crypto" if a.crypto else "stock" if a.stock else "auto", currency=a.currency)
    elif wtype == "web":
        if len(rest) != 1:
            raise WatchError("web needs one URL (quote it)")
        p.update(url=rest[0], selector=a.selector, text=a.text, mode=a.mode)
    elif wtype == "youtube":
        p["channel"] = " ".join(rest)
    elif wtype == "team":
        if not rest:
            raise WatchError("team needs a league and a team, like: add team nhl \"Maple Leafs\"")
        for i in range(len(rest), 0, -1):
            if league_key(" ".join(rest[:i])):
                p.update(league=" ".join(rest[:i]), team=" ".join(rest[i:]), start=a.start)
                break
        else:
            raise WatchError(f"don't know the league {rest[0]}; try " + ", ".join(LEAGUES))
    return p


def _list_line(w: dict) -> str:
    bits = [f"{w['id']} [{w['status']}] {w['type']}: {w['label']}", describe(w)]
    if w.get("last_value"):
        bits.append(f"now {w['last_value']}")
    bits.append(f"last fired {when(w.get('last_fired'))}")
    if w.get("last_error"):
        bits.append(f"ERROR {w['last_error']}")
    return " | ".join(bits)


def main(argv=None) -> int:
    ap = _Parser(prog="watch.py", description="Atlas's watches: email, price, web, youtube, team.")
    sub = ap.add_subparsers(dest="cmd")
    add_p = sub.add_parser("add", help="add a watch")
    add_p.add_argument("type", choices=TYPES)
    add_p.add_argument("rest", nargs="*")
    add_p.add_argument("--label", default="")
    add_p.add_argument("--selector", default="")
    add_p.add_argument("--text", default="")
    add_p.add_argument("--mode", default="")
    add_p.add_argument("--crypto", action="store_true")
    add_p.add_argument("--stock", action="store_true")
    add_p.add_argument("--currency", default="usd")
    add_p.add_argument("--start", action="store_true", help="team: also when games start")
    ls = sub.add_parser("list", help="list watches")
    ls.add_argument("--all", action="store_true", help="include paused and done")
    for name in ("done", "pause", "resume", "remove"):
        sub.add_parser(name).add_argument("id")
    ck = sub.add_parser("check", help="check now and print what it found (uses up nothing)")
    ck.add_argument("id", nargs="?")
    a = ap.parse_args(argv)
    if not a.cmd:
        print(USAGE)
        return 0
    try:
        if a.cmd == "add":
            _, msg = add(a.type, _cli_params(a.type, a.rest, a))
            print(msg)
        elif a.cmd == "list":
            rows = load(include_all=a.all)
            if not rows:
                print("No watches." if a.all else "No active watches.")
            for w in rows:
                print(_list_line(w))
        elif a.cmd in ("done", "pause", "resume"):
            print(set_status(a.id, {"done": "done", "pause": "paused", "resume": "active"}[a.cmd])[1])
        elif a.cmd == "remove":
            print(remove(a.id))
        elif a.cmd == "check":
            rows = check(a.id)
            if not rows:
                print("No active watches.")
            failed = False
            for _, line in rows:
                print(line)
                failed = failed or "CHECK FAILED" in line
            return 1 if failed else 0
    except WatchError as e:
        print(("NOT SET: " if a.cmd == "add" else "ERROR: ") + str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
