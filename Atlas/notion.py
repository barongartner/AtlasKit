r"""Notion for Atlas, through the owner's internal integration.

Usage:
  python notion.py search ["words"]               pages and databases Atlas can see (all if no words)
  python notion.py read "page title, link or id"  the page as plain text, child pages listed
  python notion.py append "page" "text" [--todo | --bullet | --heading]
                                                  add a line to the end of a page
  python notion.py addrow "database" "Title" [--prop "Name=value" ...]
                                                  add a row; props match the database's columns
  python notion.py columns "database"             a database's columns and their types
  python notion.py test                           check the key and count what's shared

Notion only shows the integration what was shared with it: on each page or
database, the ... menu, Connections, add the integration. A "can't find"
usually means it isn't shared yet.
Dates are YYYY-MM-DD, checkboxes yes/no, multi-select comma separated.
Setting (Shared\.env): NOTION_TOKEN, the Internal Integration Secret.
"""

import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Shared"))
import addons  # noqa: E402

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"


class NotionError(Exception):
    pass


def call(method: str, path: str, body=None, params=None) -> dict:
    token = addons.setting("NOTION_TOKEN")
    if not token:
        raise NotionError("Notion isn't connected. Paste the integration secret in the Atlas app, Add-ons, Notion.")
    try:
        r = requests.request(method, API + path, json=body, params=params, timeout=25,
                             headers={"Authorization": f"Bearer {token}", "Notion-Version": VERSION})
    except requests.RequestException as e:
        raise NotionError(f"Couldn't reach Notion: {type(e).__name__}.")
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code == 401:
        raise NotionError("Notion key rejected. Copy the Internal Integration Secret again from notion.so/profile/integrations.")
    if r.status_code == 404 or j.get("code") == "object_not_found":
        raise NotionError("Notion can't find that. It's probably not shared with the integration: "
                          "open it in Notion, ... menu, Connections, add it.")
    if r.status_code == 429:
        raise NotionError("Notion says slow down. Try again in a minute.")
    if r.status_code >= 400:
        raise NotionError(f"Notion said {r.status_code}: {j.get('message') or r.text[:150]}")
    return j


# ---------------------------------------------------------------- lookup --

def notion_id(ref: str) -> str:
    """The 32-hex id from a Notion link (.../My-Page-<id>?v=...) or a bare id, else ''."""
    last = ref.strip().split("?")[0].split("#")[0].rstrip("/").split("/")[-1].replace("-", "")
    tail = last[-32:]
    return tail.lower() if len(tail) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", tail) else ""


def plain(rich: list) -> str:
    return "".join(t.get("plain_text", "") for t in rich or [])


def title_of(obj: dict) -> str:
    if obj.get("object") == "database":
        return plain(obj.get("title")) or "(untitled database)"
    for p in (obj.get("properties") or {}).values():
        if p.get("type") == "title":
            return plain(p.get("title")) or "(untitled)"
    return "(untitled)"


def search(words: str = "", kind: str = "") -> list[dict]:
    body = {"page_size": 50}
    if words:
        body["query"] = words
    if kind:
        body["filter"] = {"property": "object", "value": kind}
    return call("POST", "/search", body).get("results", [])


def resolve(ref: str, kind: str) -> dict:
    """A page or database from a link, an id, or (part of) its title."""
    raw = notion_id(ref)
    if raw:
        return call("GET", f"/{kind}s/{raw}")
    hits = [h for h in search(ref, kind) if not h.get("archived")]
    exact = [h for h in hits if title_of(h).lower() == ref.lower()]
    hits = exact or [h for h in hits if ref.lower() in title_of(h).lower()] or hits
    if not hits:
        raise NotionError(f"No {kind} called {ref!r} is shared with the integration.")
    if len(hits) > 1 and not exact:
        names = "; ".join(f"{title_of(h)} ({h['id']})" for h in hits[:6])
        raise NotionError(f"More than one {kind} matches {ref!r}: {names}. Use the id.")
    return hits[0]


# ------------------------------------------------------------------ read --

def block_text(b: dict, depth: int) -> list[str]:
    t = b.get("type", "")
    data = b.get(t) or {}
    pad = "  " * depth
    text = plain(data.get("rich_text"))
    if t == "child_page":
        return [f"{pad}[page] {data.get('title', '')}"]
    if t == "child_database":
        return [f"{pad}[database] {data.get('title', '')}"]
    if t in ("heading_1", "heading_2", "heading_3"):
        return [f"{pad}{text.upper() if t == 'heading_1' else text}"]
    if t in ("bulleted_list_item", "toggle"):
        return [f"{pad}- {text}"]
    if t == "numbered_list_item":
        return [f"{pad}# {text}"]
    if t == "to_do":
        return [f"{pad}[{'x' if data.get('checked') else ' '}] {text}"]
    if t in ("quote", "callout"):
        return [f"{pad}> {text}"]
    if t == "code":
        return [f"{pad}{line}" for line in text.splitlines()]
    if t == "divider":
        return [f"{pad}---"]
    if t in ("bookmark", "embed", "link_preview"):
        return [f"{pad}{data.get('url', '')}"]
    if t in ("image", "file", "pdf", "video"):
        return [f"{pad}[{t}] {plain(data.get('caption'))}".rstrip()]
    if t == "table_row":
        return [pad + " | ".join(plain(c) for c in data.get("cells", []))]
    return [f"{pad}{text}"] if text else []


def children(block_id: str, depth: int = 0, budget: list | None = None) -> list[str]:
    budget = budget if budget is not None else [400]   # max blocks per read, so a huge page can't hang a turn
    out, cursor = [], None
    while budget[0] > 0:
        params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        j = call("GET", f"/blocks/{block_id}/children", params=params)
        for b in j.get("results", []):
            budget[0] -= 1
            out += block_text(b, depth)
            if b.get("has_children") and b["type"] not in ("child_page", "child_database") and depth < 3:
                out += children(b["id"], depth + 1, budget)
        if not j.get("has_more"):
            break
        cursor = j.get("next_cursor")
    return out


def prop_text(p: dict) -> str:
    t = p.get("type")
    v = p.get(t)
    if t in ("title", "rich_text"):
        return plain(v)
    if t in ("select", "status"):
        return (v or {}).get("name", "")
    if t == "multi_select":
        return ", ".join(o["name"] for o in v or [])
    if t == "date":
        return ((v or {}).get("start") or "") + (f" to {v['end']}" if v and v.get("end") else "")
    if t == "checkbox":
        return "yes" if v else "no"
    if t in ("number", "url", "email", "phone_number"):
        return "" if v is None else str(v)
    if t == "formula":
        return str((v or {}).get((v or {}).get("type", ""), ""))
    return ""


# ---------------------------------------------------------------- write --

def text_blocks(text: str, kind: str) -> list[dict]:
    pieces = [text[i:i + 1900] for i in range(0, len(text), 1900)] or [""]
    out = []
    for piece in pieces:
        rich = [{"type": "text", "text": {"content": piece}}]
        data = {"rich_text": rich}
        if kind == "to_do":
            data["checked"] = False
        out.append({"object": "block", "type": kind, kind: data})
    return out


def prop_value(kind: str, raw: str, spec: dict) -> dict:
    if kind == "title":
        return {"title": [{"type": "text", "text": {"content": raw}}]}
    if kind == "rich_text":
        return {"rich_text": [{"type": "text", "text": {"content": raw}}]}
    if kind == "number":
        try:
            return {"number": float(raw) if "." in raw else int(raw)}
        except ValueError:
            raise NotionError(f"{raw!r} isn't a number.")
    if kind == "select":
        return {"select": {"name": raw}}
    if kind == "status":
        names = [o["name"] for o in (spec.get("status") or {}).get("options", [])]
        match = next((n for n in names if n.lower() == raw.lower()), None)
        if not match:
            raise NotionError(f"Status has to be one of: {', '.join(names)}")
        return {"status": {"name": match}}
    if kind == "multi_select":
        return {"multi_select": [{"name": x.strip()} for x in raw.split(",") if x.strip()]}
    if kind == "date":
        parts = [x.strip() for x in re.split(r"\s+to\s+", raw)]
        return {"date": {"start": parts[0], **({"end": parts[1]} if len(parts) > 1 else {})}}
    if kind == "checkbox":
        return {"checkbox": raw.strip().lower() in ("yes", "y", "true", "1", "x", "done")}
    if kind in ("url", "email", "phone_number"):
        return {kind: raw}
    raise NotionError(f"Atlas can't fill a {kind} column from here.")


# -------------------------------------------------------------- commands --

def cmd_search(args):
    hits = search(" ".join(args).strip())
    if not hits:
        print("Nothing found. Notion only shows what's shared with the integration.")
    for h in hits:
        kind = "database" if h["object"] == "database" else "page"
        print(f"{title_of(h)} ({kind}) | {h['id']} | {h.get('url', '')}")


def cmd_read(args):
    if not args:
        raise NotionError('notion.py read "page title, link or id"')
    try:
        page = resolve(args[0], "page")
    except NotionError:
        db = resolve(args[0], "database")
        return cmd_rows(db)
    print(f"{title_of(page)} | {page.get('url', '')}")
    props = [(k, prop_text(v)) for k, v in (page.get("properties") or {}).items() if v.get("type") != "title"]
    for k, v in props:
        if v:
            print(f"{k}: {v}")
    lines = children(page["id"])
    print("\n".join(lines) if lines else "(the page is empty)")


def cmd_rows(db: dict):
    print(f"{title_of(db)} (database) | {db.get('url', '')}")
    rows = call("POST", f"/databases/{db['id']}/query", {"page_size": 50}).get("results", [])
    if not rows:
        print("(no rows)")
    for r in rows:
        cells = [f"{k}: {prop_text(v)}" for k, v in r["properties"].items() if v.get("type") != "title" and prop_text(v)]
        print(f"- {title_of(r)}" + (f" | {'; '.join(cells)}" if cells else ""))


def cmd_append(args):
    kind = "paragraph"
    for flag, k in (("--todo", "to_do"), ("--bullet", "bulleted_list_item"), ("--heading", "heading_2")):
        if flag in args:
            args.remove(flag)
            kind = k
    if len(args) < 2:
        raise NotionError('notion.py append "page" "text"')
    page = resolve(args[0], "page")
    call("PATCH", f"/blocks/{page['id']}/children", {"children": text_blocks(" ".join(args[1:]), kind)})
    print(f"Added to {title_of(page)} | {page.get('url', '')}")


def cmd_columns(args):
    db = resolve(args[0], "database") if args else None
    if not db:
        raise NotionError('notion.py columns "database"')
    print(f"{title_of(db)} columns:")
    for name, spec in db["properties"].items():
        opts = (spec.get(spec["type"]) or {}).get("options") if isinstance(spec.get(spec["type"]), dict) else None
        extra = f" ({', '.join(o['name'] for o in opts[:12])})" if opts else ""
        print(f"{name}: {spec['type']}{extra}")


def cmd_addrow(args):
    props = []
    while "--prop" in args:
        i = args.index("--prop")
        if i + 1 >= len(args) or "=" not in args[i + 1]:
            raise NotionError('--prop needs "Column=value"')
        props.append(args[i + 1])
        del args[i:i + 2]
    if len(args) < 2:
        raise NotionError('notion.py addrow "database" "Title" [--prop "Column=value"]')
    db = resolve(args[0], "database")
    schema = db["properties"]
    title_col = next(k for k, v in schema.items() if v["type"] == "title")
    values = {title_col: prop_value("title", " ".join(args[1:]), schema[title_col])}
    for p in props:
        name, _, raw = p.partition("=")
        col = next((k for k in schema if k.lower() == name.strip().lower()), None)
        if not col:
            raise NotionError(f"No column called {name.strip()!r}. Columns: {', '.join(schema)}")
        values[col] = prop_value(schema[col]["type"], raw.strip(), schema[col])
    row = call("POST", "/pages", {"parent": {"database_id": db["id"]}, "properties": values})
    print(f"Added {title_of(row)} to {title_of(db)} | {row.get('url', '')}")


def cmd_test(_args):
    me = call("GET", "/users/me")
    hits = search()
    pages = sum(1 for h in hits if h["object"] == "page")
    dbs = sum(1 for h in hits if h["object"] == "database")
    name = me.get("name") or "the integration"
    if not hits:
        print(f"Notion key works ({name}), but nothing is shared with it yet. In Notion: ... menu, Connections, add it.")
    else:
        more = "+" if len(hits) >= 50 else ""
        print(f"Notion connected ({name}). It can see {pages}{more} pages and {dbs} databases.")


COMMANDS = {"search": cmd_search, "read": cmd_read, "append": cmd_append, "addrow": cmd_addrow,
            "columns": cmd_columns, "test": cmd_test}


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        return 1
    if argv[0] != "test":
        addons.require("notion")
    try:
        COMMANDS[argv[0]](argv[1:])
    except NotionError as e:
        print(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
