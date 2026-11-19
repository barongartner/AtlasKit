r"""Apple Music for Atlas.

Works with no setup (Apple's free iTunes Search API):
  python apple_music.py search "words" [--album | --artist] [-n 5]   songs (or albums, artists) with links
  python apple_music.py link "song words"                           the top song's Apple Music link

Needs the Apple Music sign-in from the Atlas app (Add-ons, Apple Music, Sign in):
  python apple_music.py recent [n]                   songs played lately (default 10)
  python apple_music.py library "words"              search the owner's own library
  python apple_music.py playlists                    the owner's playlists
  python apple_music.py newplaylist "Name" [--desc "words"]
  python apple_music.py addtracks "playlist name or id" "song words" ["more" ...]
  python apple_music.py test                         what works right now

The sign-in keeps the web player's own tokens (APPLE_MUSIC_DEV_TOKEN and
APPLE_MUSIC_USER_TOKEN) in Shared\.env. Apple expires them every few months;
when that happens every library command says so and the owner signs in again
from the app. Search and links keep working either way.
"""

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Shared"))
import addons  # noqa: E402

ITUNES = "https://itunes.apple.com/search"
AMP = "https://amp-api.music.apple.com"
EXPIRED = ("The Apple Music sign-in expired. Sign in again in the Atlas app: Add-ons, Apple Music, Sign in. "
           "Search and song links still work.")
NOT_SIGNED_IN = ("Apple Music isn't signed in, so the library and playlists are off. Sign in from the Atlas app: "
                 "Add-ons, Apple Music, Sign in. Search and song links still work.")


class MusicError(Exception):
    pass


def country() -> str:
    return (addons.setting("APPLE_MUSIC_COUNTRY") or addons.setting("APPLE_MUSIC_STOREFRONT") or "us").lower()[:2]


# ---------------------------------------------------------------- search --

def itunes(term: str, entity: str = "song", n: int = 5) -> list[dict]:
    try:
        # a tiny limit makes the search come back short or empty, so ask for more and trim
        r = requests.get(ITUNES, params={"term": term, "media": "music", "entity": entity,
                                         "limit": min(max(n * 3, 15), 50), "country": country()}, timeout=20)
    except requests.RequestException as e:
        raise MusicError(f"Couldn't reach Apple's search: {type(e).__name__}.")
    if r.status_code >= 400:
        raise MusicError(f"Apple's search said {r.status_code}.")
    try:
        return r.json().get("results", [])[:max(1, min(n, 25))]
    except ValueError:
        raise MusicError("Apple's search sent back something unreadable. Try again.")


def song_line(x: dict, n: int | None = None) -> str:
    head = f"{n}. " if n is not None else ""
    year = (x.get("releaseDate") or "")[:4]
    album = x.get("collectionName", "")
    return (f"{head}{x.get('trackName')} - {x.get('artistName')}"
            + (f" ({album}{', ' + year if year else ''})" if album else "") + f" | {x.get('trackViewUrl', '')}")


def cmd_search(args):
    entity = "song"
    if "--album" in args:
        args.remove("--album")
        entity = "album"
    if "--artist" in args:
        args.remove("--artist")
        entity = "musicArtist"
    n = 5
    if "-n" in args:
        i = args.index("-n")
        n = int(args[i + 1])
        del args[i:i + 2]
    words = " ".join(args).strip()
    if not words:
        raise MusicError('apple_music.py search "words"')
    hits = itunes(words, entity, n)
    if not hits:
        print(f"Nothing on Apple Music for {words!r}.")
    for i, x in enumerate(hits, 1):
        if entity == "song":
            print(song_line(x, i))
        elif entity == "album":
            print(f"{i}. {x.get('collectionName')} - {x.get('artistName')} ({(x.get('releaseDate') or '')[:4]}, "
                  f"{x.get('trackCount', '?')} songs) | {x.get('collectionViewUrl', '')}")
        else:
            print(f"{i}. {x.get('artistName')} ({x.get('primaryGenreName', '')}) | {x.get('artistLinkUrl', '')}")


def cmd_link(args):
    hits = itunes(" ".join(args).strip(), "song", 1)
    if not hits:
        raise MusicError("No song found for that.")
    print(song_line(hits[0]))


# --------------------------------------------------------------- library --

def amp(method: str, path: str, params=None, body=None) -> dict:
    dev, user = addons.setting("APPLE_MUSIC_DEV_TOKEN"), addons.setting("APPLE_MUSIC_USER_TOKEN")
    if not dev or not user:
        raise MusicError(NOT_SIGNED_IN)
    try:
        r = requests.request(method, AMP + path, params=params, json=body, timeout=20, headers={
            "Authorization": f"Bearer {dev}", "Media-User-Token": user,
            "Origin": "https://music.apple.com", "Referer": "https://music.apple.com/"})
    except requests.RequestException as e:
        raise MusicError(f"Couldn't reach Apple Music: {type(e).__name__}.")
    if r.status_code in (401, 403):
        raise MusicError(EXPIRED)
    if r.status_code == 429:
        raise MusicError("Apple Music says slow down. Try again in a minute.")
    if r.status_code >= 400:
        try:
            err = (r.json().get("errors") or [{}])[0]
            msg = err.get("detail") or err.get("title") or r.text[:120]
        except ValueError:
            msg = r.text[:120]
        raise MusicError(f"Apple Music said {r.status_code}: {msg}")
    if not r.content:
        return {}
    try:
        return r.json()
    except ValueError:
        return {}


def storefront() -> str:
    sf = addons.setting("APPLE_MUSIC_STOREFRONT")
    if sf:
        return sf
    data = amp("GET", "/v1/me/storefront").get("data") or [{}]
    sf = data[0].get("id", "") or "us"
    addons.save({"APPLE_MUSIC_STOREFRONT": sf})
    return sf


def lib_line(item: dict, n: int | None = None) -> str:
    a = item.get("attributes") or {}
    head = f"{n}. " if n is not None else ""
    url = (a.get("url") or "")
    return f"{head}{a.get('name')} - {a.get('artistName', '')}" + (f" ({a['albumName']})" if a.get("albumName") else "") + (f" | {url}" if url else "")


def cmd_recent(args):
    n = int(args[0]) if args and args[0].isdigit() else 10
    items = amp("GET", "/v1/me/recent/played/tracks", {"limit": max(1, min(n, 30)), "types": "songs,library-songs"}).get("data", [])
    if not items:
        print("No recent plays on Apple Music.")
    for i, it in enumerate(items, 1):
        print(lib_line(it, i))


def cmd_library(args):
    words = " ".join(args).strip()
    if not words:
        raise MusicError('apple_music.py library "words"')
    res = amp("GET", "/v1/me/library/search", {"term": words, "types": "library-songs,library-albums,library-playlists", "limit": 10})
    res = res.get("results") or {}
    found = False
    for key, label in (("library-songs", "Song"), ("library-albums", "Album"), ("library-playlists", "Playlist")):
        for it in (res.get(key) or {}).get("data", []):
            found = True
            print(f"{label}: {lib_line(it)}")
    if not found:
        print(f"Nothing in the library for {words!r}.")


def all_playlists() -> list[dict]:
    out, path, params = [], "/v1/me/library/playlists", {"limit": 100}
    while path and len(out) < 500:
        j = amp("GET", path, params)
        out += j.get("data", [])
        path, params = j.get("next"), None
    return out


def find_playlist(ref: str) -> dict:
    if ref.startswith("p."):
        return {"id": ref, "attributes": {"name": ref}}
    mine = all_playlists()
    exact = [p for p in mine if (p.get("attributes") or {}).get("name", "").lower() == ref.lower()]
    near = exact or [p for p in mine if ref.lower() in (p.get("attributes") or {}).get("name", "").lower()]
    if not near:
        raise MusicError(f"No playlist of yours called {ref!r}. Make it with newplaylist first.")
    if len(near) > 1 and not exact:
        raise MusicError("More than one playlist matches: " + "; ".join(p["attributes"]["name"] for p in near[:6]))
    if not (near[0].get("attributes") or {}).get("canEdit", True):
        raise MusicError(f"{near[0]['attributes']['name']} isn't yours to edit.")
    return near[0]


def cmd_playlists(_args):
    for p in all_playlists():
        a = p.get("attributes") or {}
        print(f"{a.get('name')} | {p['id']}" + ("" if a.get("canEdit", True) else " (not editable)"))


def cmd_newplaylist(args):
    desc = ""
    if "--desc" in args:
        i = args.index("--desc")
        desc = args[i + 1]
        del args[i:i + 2]
    name = " ".join(args).strip()
    if not name:
        raise MusicError('apple_music.py newplaylist "Name"')
    attrs = {"name": name, **({"description": desc} if desc else {})}
    j = amp("POST", "/v1/me/library/playlists", body={"attributes": attrs})
    pid = (j.get("data") or [{}])[0].get("id", "")
    print(f"Made the playlist {name} in Apple Music | {pid}. It shows up on the phone within a minute or two.")


def catalog_song(words: str) -> dict:
    sf = storefront()
    res = amp("GET", f"/v1/catalog/{sf}/search", {"term": words, "types": "songs", "limit": 1})
    hits = ((res.get("results") or {}).get("songs") or {}).get("data", [])
    if not hits:
        raise MusicError(f"No song on Apple Music for {words!r}.")
    return hits[0]


def cmd_addtracks(args):
    if len(args) < 2:
        raise MusicError('apple_music.py addtracks "playlist" "song words" ["more" ...]')
    pl = find_playlist(args[0])
    songs = [catalog_song(w) for w in args[1:]]
    amp("POST", f"/v1/me/library/playlists/{pl['id']}/tracks",
        body={"data": [{"id": s["id"], "type": "songs"} for s in songs]})
    print(f"Added to {pl['attributes']['name']}:")
    for s in songs:
        print(lib_line(s))


def cmd_test(_args):
    hits = itunes("Bohemian Rhapsody", "song", 1)
    if not hits:
        raise MusicError("Apple's search answered but found nothing for a test song.")
    if not addons.setting("APPLE_MUSIC_USER_TOKEN"):
        print("Search and links work. Not signed in, so no library or playlists yet.")
        return
    amp("GET", "/v1/me/library/playlists", {"limit": 1})
    print(f"Search, library and playlists all work (store: {storefront()}).")


COMMANDS = {"search": cmd_search, "link": cmd_link, "recent": cmd_recent, "library": cmd_library,
            "playlists": cmd_playlists, "newplaylist": cmd_newplaylist, "addtracks": cmd_addtracks, "test": cmd_test}


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        return 1
    if argv[0] != "test":
        addons.require("apple_music")
    try:
        COMMANDS[argv[0]](argv[1:])
    except MusicError as e:
        print(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
