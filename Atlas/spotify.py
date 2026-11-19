r"""Spotify for Atlas, through the owner's own Spotify developer app.

Usage:
  python spotify.py now                        what's playing right now, and on which device
  python spotify.py recent [n]                 the last n songs played (default 10)
  python spotify.py search "words" [--type track|album|artist|playlist] [-n 5]
  python spotify.py play ["song words" | spotify:uri]   resume, or play that
  python spotify.py pause | skip | back        on the active device
  python spotify.py queue "song words" | spotify:track:...
  python spotify.py devices                    phones, speakers and PCs Spotify sees
  python spotify.py playlists                  the owner's playlists
  python spotify.py newplaylist "Name" [--public] [--desc "words"]
  python spotify.py addtracks "playlist name or id" "song words" ["more song words" ...]
  python spotify.py test | login               check the connection; sign in from a terminal
Play, pause, skip, back and queue need Spotify Premium and a device that's on
(Spotify open on the phone or PC). Every song line ends with its link.

Settings (Shared\.env): SPOTIFY_CLIENT_ID from the owner's app at
developer.spotify.com, with the redirect URI from Shared\addons.py. Connect
in the Atlas app stores SPOTIFY_REFRESH_TOKEN; access tokens refresh here.
"""

import base64
import hashlib
import secrets
import sys
import time
import urllib.parse
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Shared"))
import addons  # noqa: E402

API = "https://api.spotify.com/v1"
AUTH = "https://accounts.spotify.com/authorize"
TOKEN = "https://accounts.spotify.com/api/token"
SCOPES = ("user-read-currently-playing user-read-playback-state user-modify-playback-state "
          "user-read-recently-played playlist-read-private playlist-modify-private playlist-modify-public")
RECONNECT = "Spotify needs connecting again: Atlas app, Add-ons, Spotify, Connect."


class SpotifyError(Exception):
    pass


# ------------------------------------------------------------------ auth --

def oauth_start(redirect_uri: str, state: str) -> tuple[str, dict]:
    client_id = addons.setting("SPOTIFY_CLIENT_ID")
    if not client_id:
        raise SpotifyError("Paste the Client ID from your Spotify app first.")
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = {"client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri, "scope": SCOPES,
         "state": state, "code_challenge_method": "S256", "code_challenge": challenge}
    return f"{AUTH}?{urllib.parse.urlencode(q)}", {"verifier": verifier, "client_id": client_id}


def _token_request(data: dict) -> dict:
    try:
        r = requests.post(TOKEN, data=data, timeout=20)
    except requests.RequestException as e:
        raise SpotifyError(f"Couldn't reach Spotify: {type(e).__name__}.")
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 400:
        err = j.get("error", "")
        if err == "invalid_client":
            raise SpotifyError("Spotify rejected the Client ID. Copy it again from your app's Settings.")
        if err == "invalid_grant":
            raise SpotifyError(RECONNECT)
        if "redirect" in (j.get("error_description") or "").lower():
            raise SpotifyError(f"The redirect URI in your Spotify app has to be exactly {addons.REDIRECT_URI}")
        raise SpotifyError(f"Spotify said {r.status_code}: {j.get('error_description') or r.text[:120]}")
    return j


def _store(j: dict) -> None:
    vals = {"SPOTIFY_ACCESS_TOKEN": j["access_token"],
            "SPOTIFY_TOKEN_EXPIRES": str(int(time.time()) + int(j.get("expires_in", 3600)) - 60)}
    if j.get("refresh_token"):  # PKCE refresh tokens can rotate
        vals["SPOTIFY_REFRESH_TOKEN"] = j["refresh_token"]
    addons.save(vals)


def oauth_finish(params: dict, ctx: dict, redirect_uri: str) -> str:
    j = _token_request({"grant_type": "authorization_code", "code": params.get("code", ""),
                        "redirect_uri": redirect_uri, "client_id": ctx["client_id"],
                        "code_verifier": ctx["verifier"]})
    _store(j)
    addons.save({"ADDON_SPOTIFY": "on"})
    me = call("GET", "/me")
    return f"Spotify connected as {me.get('display_name') or me.get('id')}."


def access_token(force: bool = False) -> str:
    tok = addons.setting("SPOTIFY_ACCESS_TOKEN")
    exp = int(addons.setting("SPOTIFY_TOKEN_EXPIRES") or 0)
    if tok and not force and time.time() < exp:
        return tok
    refresh = addons.setting("SPOTIFY_REFRESH_TOKEN")
    client_id = addons.setting("SPOTIFY_CLIENT_ID")
    if not refresh or not client_id:
        raise SpotifyError("Spotify isn't connected. Atlas app, Add-ons, Spotify, Connect.")
    j = _token_request({"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id})
    _store(j)
    return j["access_token"]


# ------------------------------------------------------------------- api --

def call(method: str, path: str, params=None, body=None, retry: bool = True):
    try:
        r = requests.request(method, API + path, params=params, json=body, timeout=20,
                             headers={"Authorization": f"Bearer {access_token()}"})
    except requests.RequestException as e:
        raise SpotifyError(f"Couldn't reach Spotify: {type(e).__name__}.")
    if r.status_code == 401 and retry:
        access_token(force=True)
        return call(method, path, params, body, retry=False)
    if r.status_code == 204 or not r.content:
        return {}
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code >= 400:
        err = j.get("error") if isinstance(j.get("error"), dict) else {"message": str(j.get("error", ""))}
        reason, msg = err.get("reason", ""), err.get("message", "") or r.text[:120]
        if r.status_code == 401:
            raise SpotifyError(RECONNECT)
        if reason == "PREMIUM_REQUIRED" or "premium" in msg.lower():
            raise SpotifyError("That needs Spotify Premium. Search, recent songs and playlists still work.")
        if reason == "NO_ACTIVE_DEVICE" or (r.status_code == 404 and path.startswith("/me/player")):
            raise SpotifyError("No Spotify device is on. Open Spotify on the phone or PC, play anything, then try again.")
        if r.status_code == 403 and "user may not be registered" in msg.lower():
            raise SpotifyError("Spotify blocked this account for the app. In the Spotify dashboard, open the app, "
                               "User Management, and add your Spotify email.")
        if r.status_code == 429:
            raise SpotifyError("Spotify says slow down. Try again in a minute.")
        raise SpotifyError(f"Spotify said {r.status_code}: {msg}")
    return j


def track_line(t: dict, n: int | None = None) -> str:
    artists = ", ".join(a["name"] for a in t.get("artists", []))
    album = (t.get("album") or {}).get("name", "")
    link = (t.get("external_urls") or {}).get("spotify", "")
    head = f"{n}. " if n is not None else ""
    return f"{head}{t.get('name')} - {artists}" + (f" ({album})" if album else "") + f" | {t.get('uri')} | {link}"


def find_track(words: str) -> dict:
    if words.startswith("spotify:track:"):
        return call("GET", f"/tracks/{words.split(':')[-1]}")
    if "open.spotify.com/track/" in words:
        return call("GET", "/tracks/" + words.split("/track/")[1].split("?")[0])
    hits = call("GET", "/search", {"q": words, "type": "track", "limit": 1}).get("tracks", {}).get("items", [])
    if not hits:
        raise SpotifyError(f"Nothing on Spotify for {words!r}.")
    return hits[0]


def find_playlist(name: str) -> dict:
    if name.startswith("spotify:playlist:") or "open.spotify.com/playlist/" in name or (len(name) == 22 and name.isalnum()):
        pid = name.split(":")[-1].split("/playlist/")[-1].split("?")[0]
        return call("GET", f"/playlists/{pid}", {"fields": "id,name,external_urls,uri"})
    mine, url = [], "/me/playlists"
    params = {"limit": 50}
    while url and len(mine) < 500:
        page = call("GET", url, params)
        mine += page.get("items", [])
        nxt = page.get("next")
        url, params = (nxt.replace(API, ""), None) if nxt else (None, None)
    exact = [p for p in mine if p and p["name"].lower() == name.lower()]
    near = exact or [p for p in mine if p and name.lower() in p["name"].lower()]
    if not near:
        raise SpotifyError(f"No playlist of yours called {name!r}. Make it with newplaylist first.")
    if len(near) > 1 and not exact:
        raise SpotifyError("More than one playlist matches: " + "; ".join(p["name"] for p in near[:6]))
    return near[0]


def pop(args: list, flag: str, has_value: bool = True):
    if flag not in args:
        return None
    i = args.index(flag)
    if not has_value:
        args.pop(i)
        return True
    if i + 1 >= len(args):
        raise SpotifyError(f"{flag} needs a value.")
    args.pop(i)
    return args.pop(i)


# --------------------------------------------------------------- commands --

def cmd_now(_args):
    j = call("GET", "/me/player", {"additional_types": "track,episode"})
    if not j or not j.get("item"):
        print("Nothing is playing on Spotify right now.")
        return
    item, dev = j["item"], j.get("device") or {}
    state = "Playing" if j.get("is_playing") else "Paused"
    pos, dur = j.get("progress_ms", 0) // 1000, item.get("duration_ms", 0) // 1000
    if item.get("type") == "episode":
        print(f"{state}: {item['name']} ({(item.get('show') or {}).get('name', 'podcast')}) on {dev.get('name', 'a device')}")
    else:
        print(f"{state} on {dev.get('name', 'a device')}, {pos // 60}:{pos % 60:02d} of {dur // 60}:{dur % 60:02d}:")
        print(track_line(item))


def cmd_recent(args):
    n = int(args[0]) if args and args[0].isdigit() else 10
    items = call("GET", "/me/player/recently-played", {"limit": max(1, min(n, 50))}).get("items", [])
    if not items:
        print("No recent plays on Spotify.")
    for i, it in enumerate(items, 1):
        print(track_line(it["track"], i) + f" | played {it.get('played_at', '')[:16].replace('T', ' ')} UTC")


def cmd_search(args):
    kind = pop(args, "--type") or "track"
    n = int(pop(args, "-n") or 5)
    words = " ".join(args).strip()
    if not words:
        raise SpotifyError('Say what to search for: spotify.py search "words"')
    res = call("GET", "/search", {"q": words, "type": kind, "limit": max(1, min(n, 20))})
    items = (res.get(kind + "s") or {}).get("items", [])
    items = [x for x in items if x]
    if not items:
        print(f"Nothing on Spotify for {words!r}.")
    for i, x in enumerate(items, 1):
        link = (x.get("external_urls") or {}).get("spotify", "")
        if kind == "track":
            print(track_line(x, i))
        elif kind == "album":
            print(f"{i}. {x['name']} - {', '.join(a['name'] for a in x['artists'])} ({x.get('release_date', '')[:4]}) | {x['uri']} | {link}")
        elif kind == "playlist":
            print(f"{i}. {x['name']} by {(x.get('owner') or {}).get('display_name', '?')} | {x['uri']} | {link}")
        else:
            print(f"{i}. {x['name']} | {x['uri']} | {link}")


def cmd_play(args):
    words = " ".join(args).strip()
    if not words:
        call("PUT", "/me/player/play")
        print("Playing.")
        return
    if words.startswith(("spotify:album:", "spotify:playlist:", "spotify:artist:")):
        call("PUT", "/me/player/play", body={"context_uri": words})
        print(f"Playing {words}.")
        return
    t = find_track(words)
    call("PUT", "/me/player/play", body={"uris": [t["uri"]]})
    print("Playing " + track_line(t))


def cmd_simple(path: str, method: str, done: str):
    def run(_args):
        call(method, path)
        print(done)
    return run


def cmd_queue(args):
    t = find_track(" ".join(args).strip())
    call("POST", "/me/player/queue", {"uri": t["uri"]})
    print("Queued " + track_line(t))


def cmd_devices(_args):
    devs = call("GET", "/me/player/devices").get("devices", [])
    if not devs:
        print("Spotify sees no devices. Open Spotify on the phone or PC.")
    for d in devs:
        print(f"{d['name']} ({d['type']}){' - active' if d.get('is_active') else ''}")


def cmd_playlists(_args):
    items = call("GET", "/me/playlists", {"limit": 50}).get("items", [])
    for p in items:
        if p:
            print(f"{p['name']} ({(p.get('tracks') or p.get('items') or {}).get('total', '?')} songs) | {p['uri']} | {p['external_urls']['spotify']}")


def cmd_newplaylist(args):
    public = bool(pop(args, "--public", has_value=False))
    desc = pop(args, "--desc") or ""
    name = " ".join(args).strip()
    if not name:
        raise SpotifyError('Give it a name: spotify.py newplaylist "Name"')
    body = {"name": name, "public": public, "description": desc}
    try:
        p = call("POST", "/me/playlists", body=body)
    except SpotifyError as e:
        if "404" not in str(e) and "405" not in str(e):
            raise
        me = call("GET", "/me")  # older API shape
        p = call("POST", f"/users/{me['id']}/playlists", body=body)
    print(f"Made the playlist {p['name']} | {p['uri']} | {p['external_urls']['spotify']}")


def cmd_addtracks(args):
    if len(args) < 2:
        raise SpotifyError('spotify.py addtracks "playlist" "song words" ["more" ...]')
    pl = find_playlist(args[0])
    tracks = [find_track(w) for w in args[1:]]
    body = {"uris": [t["uri"] for t in tracks]}
    try:
        call("POST", f"/playlists/{pl['id']}/items", body=body)
    except SpotifyError as e:
        if "404" not in str(e) and "405" not in str(e):
            raise
        call("POST", f"/playlists/{pl['id']}/tracks", body=body)
    print(f"Added to {pl['name']} ({pl['external_urls']['spotify']}):")
    for t in tracks:
        print(track_line(t))


def cmd_test(_args):
    me = call("GET", "/me")
    print(f"Spotify connected as {me.get('display_name') or me.get('id')}"
          f" ({'Premium' if me.get('product') == 'premium' else 'free account: no play, pause or skip'}).")


COMMANDS = {
    "now": cmd_now, "recent": cmd_recent, "search": cmd_search, "play": cmd_play,
    "pause": cmd_simple("/me/player/pause", "PUT", "Paused."),
    "skip": cmd_simple("/me/player/next", "POST", "Skipped."),
    "next": cmd_simple("/me/player/next", "POST", "Skipped."),
    "back": cmd_simple("/me/player/previous", "POST", "Back one song."),
    "queue": cmd_queue, "devices": cmd_devices, "playlists": cmd_playlists,
    "newplaylist": cmd_newplaylist, "addtracks": cmd_addtracks, "test": cmd_test,
}


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not argv or argv[0] not in COMMANDS and argv[0] != "login":
        print(__doc__)
        return 1
    if argv[0] == "login":
        addons.oauth_login_cli("spotify")
        return 0
    if argv[0] != "test":
        addons.require("spotify")
    try:
        COMMANDS[argv[0]](argv[1:])
    except SpotifyError as e:
        print(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
