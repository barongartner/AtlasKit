r"""Voice notes: local speech-to-text with faster-whisper, on this PC's CPU.

Usage:
  python voice_notes.py transcribe "file"   print what's said in an audio file (m4a, caf, ogg, mp3, wav...)
  python voice_notes.py status              is it ready: package, ffmpeg, model
  python voice_notes.py download            fetch the model now so the first voice note isn't slow

The bot (atlas_bot.py) uses transcribe() for every voice memo the owner texts
and hands Atlas "[Voice note] <transcript>". With ADDON_VOICE_NOTES=off it
hands over the saved file instead. The model is cached in the kit's Models
folder, not the user profile, so it moves with the kit and can be deleted
with it. VOICE_NOTES_MODEL picks it: base.en (default), small.en, base, small.
"""

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent / "Shared"))
import addons  # noqa: E402

MODELS = addons.ROOT / "Models" / "whisper"
NO_WINDOW = 0x08000000
ALLOWED = {"tiny", "tiny.en", "base", "base.en", "small", "small.en"}
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")  # Windows without developer mode can't symlink

_model = None
_model_name = ""
_lock = threading.Lock()


def model_name() -> str:
    name = (addons.setting("VOICE_NOTES_MODEL") or "base.en").strip().lower()
    return name if name in ALLOWED else "base.en"


def enabled() -> bool:
    return addons.enabled(addons.BY_ID["voice_notes"])


def cached(name: str | None = None) -> bool:
    from faster_whisper.utils import download_model
    try:
        download_model(name or model_name(), local_files_only=True, cache_dir=str(MODELS))
        return True
    except Exception:  # noqa: BLE001 - any failure here just means "not downloaded yet"
        return False


def load():
    global _model, _model_name
    name = model_name()
    if _model is None or _model_name != name:
        from faster_whisper import WhisperModel
        MODELS.mkdir(parents=True, exist_ok=True)
        _model = WhisperModel(name, device="cpu", compute_type="int8", download_root=str(MODELS))
        _model_name = name
    return _model


def transcribe(path: Path) -> str:
    """Audio file -> text. Blocking; one at a time. Converts to 16 kHz mono
    WAV with ffmpeg first when ffmpeg is there (iMessage sends CAF/M4A)."""
    path = Path(path)
    wav = path.with_name(path.stem + ".16k.wav")
    src = path
    if shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-y", "-i", str(path), "-ar", "16000", "-ac", "1", str(wav)],
                       capture_output=True, timeout=120, check=True, creationflags=NO_WINDOW)
        src = wav
    try:
        with _lock:  # guards the lazy load and keeps one transcription at a time
            segments, _info = load().transcribe(str(src))
            return " ".join(seg.text.strip() for seg in segments).strip()
    finally:
        if src == wav:
            wav.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cmd = argv[0] if argv else ""
    if cmd == "transcribe" and len(argv) > 1:
        f = Path(" ".join(argv[1:]))
        if not f.exists():
            print(f"No file at {f}")
            return 1
        try:
            text = transcribe(f)
        except subprocess.CalledProcessError:
            print("ffmpeg couldn't read that audio file.")
            return 1
        except ImportError:
            print("faster-whisper isn't installed. Run Setup.cmd again.")
            return 1
        print(text or "(nothing audible in it)")
        return 0
    if cmd == "status":
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            print("faster-whisper isn't installed, so voice notes can't be typed out. Run Setup.cmd again.")
            return 1
        name = model_name()
        ff = "ffmpeg found" if shutil.which("ffmpeg") else "no ffmpeg (most files still work)"
        if cached(name):
            print(f"Ready: the {name} model is downloaded, {ff}.")
        else:
            print(f"Ready. The {name} model downloads with the first voice note (or press Download now), {ff}.")
        return 0
    if cmd == "download":
        name = model_name()
        if cached(name):
            print(f"OK: the {name} model is already here.")
            return 0
        print(f"STEP: Downloading the {name} speech model (about {'470' if 'small' in name else '140'} MB)", flush=True)
        try:
            load()
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: the download failed: {e}")
            return 1
        print("OK: Voice notes are ready.")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
