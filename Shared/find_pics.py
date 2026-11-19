r"""Pictures for the crew to send: memes, web photos, and memes made to order.

  python find_pics.py memes [words] [-n 6]      top memes from Lemmy's meme communities
                                                (this week's best, or a search with words)
  python find_pics.py photos "confused cat" [-n 4]   real photos from Pixabay (safe search on)
  python find_pics.py templates [word]          meme templates you can caption (imgflip's top 100)
  python find_pics.py meme "Template" "text 1" ["text 2" ...] [--bar]
                                                caption a template yourself. The multi-panel
                                                ones (Drake, Two Buttons, Distracted Boyfriend,
                                                Expanding Brain, Left Exit 12, Woman Yelling At
                                                Cat) put each text in its own panel; the rest
                                                get classic top/bottom text. --bar puts one
                                                caption in a white bar above the picture.
  python find_pics.py url https://site/pic.jpg  download one picture you found on the web

Pictures land in .\Pics\ under the folder you run it from (anything older than
3 days there is cleared each run) and the script prints one full path per line.
LOOK at them before sending (the Read tool shows images): skip anything that
isn't funny, doesn't fit, or isn't something the owner would want their name on.
Needs the PIXABAY_KEY setting for photos (Shared\\.env from setup, or the
.env in the folder you run it from, which wins).
"""

import argparse
import hashlib
import io
import os
import re
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

OUT = Path.cwd() / "Pics"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
LEMMY = "https://lemmy.world/api/v3"
MEME_COMMUNITIES = ["memes", "memes@lemmy.ml", "funny"]
IMAGE_URL = re.compile(r"\.(jpe?g|png|webp|gif)(\?.*)?$", re.I)
IMPACT = Path(r"C:\Windows\Fonts\impact.ttf")
ARIAL_BOLD = Path(r"C:\Windows\Fonts\arialbd.ttf")


def http() -> httpx.Client:
    return httpx.Client(headers=UA, timeout=30, follow_redirects=True)


def clean_old(days: int = 3) -> None:
    OUT.mkdir(exist_ok=True)
    cutoff = time.time() - days * 86400
    for f in OUT.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^\w\s-]", "", text).strip()
    return re.sub(r"\s+", " ", s)[:n].strip() or "Pic"


def save(c: httpx.Client, url: str, name: str) -> Path | None:
    """Download one picture; None if it isn't one or is too big to text."""
    try:
        r = c.get(url)
    except httpx.HTTPError:
        return None
    kind = r.headers.get("content-type", "").split(";")[0]
    if r.status_code >= 400 or not kind.startswith("image/") or len(r.content) > 15_000_000:
        return None
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(kind, ".jpg")
    if ext == ".webp":  # iMessage handles webp poorly; send a jpg instead
        try:
            from PIL import Image
            buf = io.BytesIO()
            Image.open(io.BytesIO(r.content)).convert("RGB").save(buf, "JPEG", quality=90)
            content, ext = buf.getvalue(), ".jpg"
        except Exception:
            content = r.content
    else:
        content = r.content
    tag = hashlib.md5(url.encode()).hexdigest()[:4]
    path = OUT / f"{slug(name)} {tag}{ext}"
    path.write_bytes(content)
    return path


STOP = {"a", "an", "the", "of", "for", "and", "or", "to", "in", "on", "with", "funny", "meme", "memes",
        "pic", "pics", "picture", "pictures", "photo", "some", "about", "that", "this", "who", "is"}


def memes(words: str, n: int) -> list[Path]:
    c = http()
    posts = []
    # Lemmy matches the whole phrase in titles, so a specific ask ("broken
    # down car") often finds nothing: fall back to its main words one by one.
    keys = list(dict.fromkeys(w for w in re.findall(r"[a-z0-9']+", words.lower()) if w not in STOP and len(w) > 2))
    tries = ([words] + keys[:4]) if words else [""]
    for q in tries:
        for comm in MEME_COMMUNITIES:
            try:
                if q:
                    r = c.get(f"{LEMMY}/search", params={"q": q, "community_name": comm, "type_": "Posts",
                                                         "sort": "TopAll", "limit": 30})
                else:
                    r = c.get(f"{LEMMY}/post/list", params={"community_name": comm, "sort": "TopWeek", "limit": 30})
                posts += r.json().get("posts", [])
            except (httpx.HTTPError, ValueError):
                continue
        if q == words and len([p for p in posts if IMAGE_URL.search(p["post"].get("url") or "")]) >= n:
            break  # the whole phrase was enough
    # Lemmy matches inside words ("car" finds "cares"): keep only titles with
    # the asked-for words as whole words, most of them first, then upvotes.
    def hits(p: dict) -> int:
        title = (p["post"].get("name") or "").lower()
        return sum(bool(re.search(rf"\b{re.escape(k)}s?\b", title)) for k in keys)
    if keys:
        posts = [p for p in posts if hits(p)]
    seen, out = set(), []
    posts.sort(key=lambda p: (-hits(p), -(p.get("counts") or {}).get("score", 0)))
    for p in posts:
        post = p["post"]
        url = post.get("url") or ""
        if post.get("nsfw") or not IMAGE_URL.search(url) or url in seen:
            continue
        seen.add(url)
        path = save(c, url, post.get("name", "Meme"))
        if path:
            out.append(path)
        if len(out) >= n:
            break
    return out


def photos(words: str, n: int) -> list[Path]:
    load_dotenv(Path.cwd() / ".env", override=True)  # the file beats the bot's stale env copy
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import owner
    key = os.getenv("PIXABAY_KEY", "").strip() or owner.get("PIXABAY_KEY")
    if not key:
        sys.exit("No PIXABAY_KEY set up, so photo search is off. Add a free Pixabay key in setup.")
    c = http()
    r = c.get("https://pixabay.com/api/", params={"key": key, "q": words[:100], "image_type": "photo",
                                                  "safesearch": "true", "per_page": max(3, min(n * 2, 50))})
    hits = r.json().get("hits", []) if r.status_code < 400 else []
    out = []
    for h in hits:
        path = save(c, h.get("largeImageURL") or h.get("webformatURL", ""), words)
        if path:
            out.append(path)
        if len(out) >= n:
            break
    return out


def templates(word: str) -> list[dict]:
    r = http().get("https://api.imgflip.com/get_memes")
    found = r.json()["data"]["memes"]
    return [m for m in found if word.lower() in m["name"].lower()] if word else found


def _fit(draw, text: str, font_path: Path, max_w: int, start: int, min_size: int = 14):
    """Biggest font size (and the wrapped lines) that fits max_w."""
    from PIL import ImageFont
    size = start
    while True:
        font = ImageFont.truetype(str(font_path), size)
        words, lines, cur = text.split(), [], ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if draw.textlength(trial, font=font) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        if (all(draw.textlength(l, font=font) <= max_w for l in lines) and len(lines) <= 4) or size <= min_size:
            return font, lines
        size -= 2


# Where each text goes on the multi-panel templates, as fractions of the
# picture (x, y, width, height), plus whether the panel is plain white
# (black text) or a photo (white outlined text). Everything else gets
# classic top/bottom text.
LAYOUTS = {
    "drake hotline bling": ("panel", [(0.52, 0.02, 0.46, 0.46), (0.52, 0.52, 0.46, 0.46)]),
    "expanding brain": ("panel", [(0.02, 0.01, 0.46, 0.23), (0.02, 0.26, 0.46, 0.23),
                                  (0.02, 0.51, 0.46, 0.23), (0.02, 0.76, 0.46, 0.23)]),
    "two buttons": ("photo", [(0.08, 0.06, 0.33, 0.14), (0.47, 0.03, 0.33, 0.14), (0.05, 0.80, 0.90, 0.18)]),
    "distracted boyfriend": ("photo", [(0.12, 0.60, 0.36, 0.22), (0.47, 0.50, 0.28, 0.20), (0.73, 0.62, 0.26, 0.22)]),
    "left exit 12 off ramp": ("photo", [(0.22, 0.10, 0.28, 0.20), (0.52, 0.10, 0.30, 0.20), (0.30, 0.70, 0.45, 0.22)]),
    "woman yelling at cat": ("photo", [(0.01, 0.02, 0.48, 0.30), (0.51, 0.02, 0.48, 0.30)]),
}
LAYOUT_HELP = {
    "drake hotline bling": "1 = what he rejects, 2 = what he likes",
    "expanding brain": "1 to 4 = dumbest idea up to galaxy brain",
    "two buttons": "1 = left button, 2 = right button, 3 = the sweating guy",
    "distracted boyfriend": "1 = the girl in red (shiny new thing), 2 = the boyfriend, 3 = the girlfriend (what he's ignoring)",
    "left exit 12 off ramp": "1 = straight on, 2 = the exit, 3 = the car swerving to it",
    "woman yelling at cat": "1 = the yelling woman, 2 = the cat",
}


def _boxed(img, texts: list[str], style: str, boxes) -> None:
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    W, H = img.size
    for text, (fx, fy, fw, fh) in zip(texts, boxes):
        if not text:
            continue
        bx, by, bw, bh = fx * W, fy * H, fw * W, fh * H
        font_path = ARIAL_BOLD if style == "panel" else IMPACT
        shown = text if style == "panel" else text.upper()
        size = int(min(bh / 2, W / 9))
        while True:
            font, lines = _fit(d, shown, font_path, int(bw), size)
            line_h = font.size + 6
            if line_h * len(lines) <= bh or font.size <= 14:
                break
            size = font.size - 2
        y = by + (bh - line_h * len(lines)) / 2
        for i, line in enumerate(lines):
            x = bx + (bw - d.textlength(line, font=font)) / 2
            if style == "panel":
                d.text((x, y + i * line_h), line, font=font, fill="black")
            else:
                d.text((x, y + i * line_h), line, font=font, fill="white",
                       stroke_width=max(2, font.size // 15), stroke_fill="black")


def meme(template: str, texts: list[str], bar: bool) -> Path:
    from PIL import Image, ImageDraw
    hits = templates(template)
    exact = [m for m in hits if m["name"].lower() == template.lower()]
    if not hits:
        sys.exit(f"No template matches {template!r}. Run: find_pics.py templates")
    m = (exact or hits)[0]
    r = http().get(m["url"])
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    if img.width < 600:
        img = img.resize((600, int(img.height * 600 / img.width)))
    W, H = img.size
    layout = LAYOUTS.get(m["name"].lower())
    top, bottom = (texts + ["", ""])[:2]
    if layout and not bar:
        _boxed(img, texts, *layout)
    elif bar:
        text = " ".join(t for t in texts if t)
        probe = ImageDraw.Draw(img)
        font, lines = _fit(probe, text, ARIAL_BOLD, W - 40, max(28, W // 14))
        line_h = font.size + 8
        pad = 24
        canvas = Image.new("RGB", (W, H + pad * 2 + line_h * len(lines)), "white")
        canvas.paste(img, (0, pad * 2 + line_h * len(lines)))
        d = ImageDraw.Draw(canvas)
        for i, line in enumerate(lines):
            d.text((20, pad + i * line_h), line, font=font, fill="black")
        img = canvas
    else:
        d = ImageDraw.Draw(img)
        for text, at_top in ((top, True), (bottom, False)):
            if not text:
                continue
            font, lines = _fit(d, text.upper(), IMPACT, W - 30, max(36, W // 9))
            line_h = font.size + 4
            y = 10 if at_top else H - 14 - line_h * len(lines)
            for i, line in enumerate(lines):
                x = (W - d.textlength(line, font=font)) / 2
                d.text((x, y + i * line_h), line, font=font, fill="white",
                       stroke_width=max(2, font.size // 15), stroke_fill="black")
    tag = hashlib.md5(f"{texts}|{time.time()}".encode()).hexdigest()[:4]
    path = OUT / f"{slug(m['name'], 30)} Meme {tag}.jpg"
    img.save(path, "JPEG", quality=90)
    return path


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("memes"); p.add_argument("words", nargs="*"); p.add_argument("-n", type=int, default=6)
    p = sub.add_parser("photos"); p.add_argument("words", nargs="+"); p.add_argument("-n", type=int, default=4)
    p = sub.add_parser("templates"); p.add_argument("word", nargs="?", default="")
    p = sub.add_parser("meme"); p.add_argument("template"); p.add_argument("texts", nargs="+")
    p.add_argument("--bar", action="store_true")
    p = sub.add_parser("url"); p.add_argument("url")
    a = ap.parse_args()
    clean_old()
    if a.cmd == "templates":
        for m in templates(a.word):
            how = LAYOUT_HELP.get(m["name"].lower(), "top and bottom text")
            print(f"{m['name']}  ({how})")
        return
    if a.cmd == "memes":
        paths = memes(" ".join(a.words), a.n)
    elif a.cmd == "photos":
        paths = photos(" ".join(a.words), a.n)
    elif a.cmd == "meme":
        paths = [meme(a.template, a.texts, a.bar)]
    else:
        paths = [p for p in [save(http(), a.url, Path(a.url.split("?")[0]).stem or "Pic")] if p]
    if not paths:
        sys.exit("Nothing found. Try other words, or another source.")
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
