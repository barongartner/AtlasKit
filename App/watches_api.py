r"""The Watches page's backend.

Mix WatchesMixin into the app's Api class and the page (watches.js) can call
every public method here as pywebview.api.watches_*(...). The watch store and
every check live in Atlas\watch.py; this is a thin layer that turns its
results and errors into plain dicts the page can show.

Every method returns {"ok": bool, ...}. On failure "error" holds one plain
sentence; nothing raises into the page.
"""

import importlib
import sys
from pathlib import Path

ATLAS = Path(__file__).resolve().parent.parent / "Atlas"


def _lib():
    """Atlas\\watch.py, imported on first use (it needs requests)."""
    if str(ATLAS) not in sys.path:
        sys.path.insert(0, str(ATLAS))
    return importlib.import_module("watch")


def _fail(e: Exception) -> dict:
    lib = sys.modules.get("watch")
    if lib is not None and isinstance(e, lib.WatchError):
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}


class WatchesMixin:
    def watches_list(self):
        """{ok, watches: [watch], types: [...], leagues: [{key, name, individual}]}.
        A watch: id, type, label, params, summary, status, created,
        last_checked, last_fired, last_value, last_note, last_error,
        error_count, next_check, plus *_when versions of the times for show."""
        try:
            lib = _lib()
            out = []
            for w in lib.load(include_all=True):
                row = lib.public(w)
                for k in ("created", "last_checked", "last_fired", "next_check"):
                    row[k + "_when"] = lib.when(row.get(k)) if row.get(k) else ""
                out.append(row)
            return {"ok": True, "watches": out, "types": list(lib.TYPES), "leagues": lib.leagues()}
        except Exception as e:  # noqa: BLE001
            return _fail(e)

    def watches_leagues(self):
        """{ok, leagues: [{key, name, individual}]} for the team form and setup."""
        try:
            return {"ok": True, "leagues": _lib().leagues()}
        except Exception as e:  # noqa: BLE001
            return _fail(e)

    def watches_add(self, type, params=None):  # noqa: A002 - the page's name for it
        """params by type (all strings unless noted, label optional on all):
        email {match, field: from|subject}; price {symbol, condition, value,
        kind: auto|stock|crypto, currency}; web {url, selector, text, mode};
        youtube {channel}; team {league, team, start: bool}.
        Returns {ok, watch, message} or {ok: False, error}."""
        try:
            lib = _lib()
            w, msg = lib.add(type, dict(params or {}))
            return {"ok": True, "watch": lib.public(w), "message": msg.removeprefix("OK: ")}
        except Exception as e:  # noqa: BLE001
            return _fail(e)

    def watches_add_teams(self, teams):
        """For the setup walkthrough: [{league, team, start?}] -> {ok, added:
        [{league, team, watch}], errors: [{league, team, error}]}. ok is True
        only when every team was added (or was already watched)."""
        added, errors = [], []
        for t in teams or []:
            t = t or {}
            league, team = str(t.get("league") or ""), str(t.get("team") or "")
            res = self.watches_add("team", {"league": league, "team": team, "start": bool(t.get("start"))})
            if res["ok"]:
                added.append({"league": league, "team": team, "watch": res["watch"]})
            else:
                errors.append({"league": league, "team": team, "error": res["error"]})
        return {"ok": not errors, "added": added, "errors": errors}

    def watches_set_status(self, id, status):  # noqa: A002
        """status: active | paused | done. Returns {ok, watch, message}."""
        try:
            lib = _lib()
            w, msg = lib.set_status(id, status)
            return {"ok": True, "watch": lib.public(w), "message": msg.removeprefix("OK: ")}
        except Exception as e:  # noqa: BLE001
            return _fail(e)

    def watches_remove(self, id):  # noqa: A002
        """Returns {ok, message}."""
        try:
            return {"ok": True, "message": _lib().remove(id).removeprefix("OK: ")}
        except Exception as e:  # noqa: BLE001
            return _fail(e)

    def watches_check(self, id):  # noqa: A002
        """Check now without using up an alert. Returns {ok, watch, message,
        would_fire: bool}; ok is False when the check itself failed (the
        watch still comes back, with last_error set)."""
        try:
            lib = _lib()
            rows = lib.check(id)
            w, line = rows[0]
            failed = "CHECK FAILED" in line
            out = {"ok": not failed, "watch": lib.public(w), "message": line,
                   "would_fire": "Would fire now" in line}
            if failed:
                out["error"] = w.get("last_error") or line
            return out
        except Exception as e:  # noqa: BLE001
            return _fail(e)
