#!/usr/bin/env python3
# Turns the JSON line from mac-info.sh (run on the owner's Intel Mac) into the
# bridge's hardware key: state/hardware-key.json and state/hardware-key.b64
# (base64 of the compact JSON), both mode 600. Same layout as the bridge's own
# tools/extract-key: the five *_enc fields stay empty because the bridge
# computes them itself on Linux; version and icloud_ua follow the Mac's macOS.
#
# Usage: make-key.py <mac-info.json | -> [--force]
#   -        read the JSON from stdin
#   --force  replace a key that's already there (the bridge's Apple
#            registration depends on it; never do this to a working bridge)
# Settings: BRIDGE_DIR (default ~/imessage-bridge).
# Output: OK: / ERROR: lines, never the values themselves. Exit 0 on success.
import base64
import json
import os
import re
import sys

PROTOCOL_VERSION = 1660
AOSKIT = "com.apple.AOSKit/282 (com.apple.accountsd/113)"
ICLOUD_UA = "com.apple.iCloudHelper/282 CFNetwork/1568.100.1 Darwin/{}"
UUID_RE = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")


def say(kind, msg):
    print(f"{kind}: {msg}", flush=True)


def fail(msg):
    say("ERROR", msg)
    sys.exit(1)


def darwin_for(version):
    """Darwin kernel version for a macOS version, when mac-info didn't send it."""
    parts = [int(x) for x in version.split(".")]
    if parts[0] == 10:
        major = parts[1] + 4                      # 10.13 -> 17, 10.15 -> 19
    elif parts[0] <= 15:
        major = parts[0] + 9                      # 11 -> 20 ... 15 -> 24
    else:
        major = parts[0] - 1                      # 26 -> 25
    return f"{major}.0.0"


def main():
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv[1:]
    if len(args) != 1:
        fail("usage: make-key.py <mac-info.json | -> [--force]")
    try:
        raw = sys.stdin.read() if args[0] == "-" else open(args[0], encoding="utf-8").read()
    except OSError as e:
        fail(f"Couldn't read {args[0]} ({e.strerror}).")
    # Tolerate stray text around the line (a pasted prompt, a trailing newline)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        fail("That isn't the output of the Mac script: no JSON in it. Run the Terminal line on the Mac again and paste everything it prints.")
    try:
        info = json.loads(m.group(0))
    except ValueError:
        fail("The Mac script's output got cut or changed on the way. Copy the whole line again.")
    if not isinstance(info, dict):
        fail("The Mac script's output isn't a JSON object. Copy the whole line again.")

    if info.get("error") == "apple_silicon" or str(info.get("cpu", "")).lower() not in ("intel", ""):
        fail("This Mac has Apple Silicon (M1 or newer). The bridge can only pose as an Intel Mac. Run the Mac script on an Intel Mac instead.")
    if info.get("error"):
        fail("The Mac script reported a problem. Run it again on the Mac.")

    def field(key, label):
        v = info.get(key)
        if not isinstance(v, str) or not v.strip():
            fail(f"The Mac didn't report its {label}. Run the Mac script again; if it stays empty, this Mac can't be used.")
        return v.strip()

    model = field("model", "model")
    if not re.fullmatch(r"[A-Za-z]+\d+,\d+", model):
        fail("The model name doesn't look like a Mac model (like MacBookPro12,1). Run the Mac script again.")
    if model.lower().startswith(("vmware", "virtualbox", "parallels")) or "vmm" in model.lower():
        fail("This is a virtual machine, not a real Mac. Apple refuses those; use a real Intel Mac.")

    mac = field("mac", "en0 network address").lower()
    if not re.fullmatch(r"[0-9a-f]{1,2}(:[0-9a-f]{1,2}){5}", mac):
        fail("The network (en0) address isn't six hex bytes. Run the Mac script again.")
    mac_bytes = [int(x, 16) for x in mac.split(":")]

    serial = field("serial", "serial number").upper()
    if not re.fullmatch(r"[A-Z0-9]{10,12}", serial):
        fail("The serial number isn't 10 to 12 letters and digits. Run the Mac script again.")

    uuid = field("uuid", "hardware UUID").upper()
    if not UUID_RE.fullmatch(uuid):
        fail("The hardware UUID isn't a valid UUID. Run the Mac script again.")

    disk = field("disk_uuid", "startup disk UUID").upper()
    if not UUID_RE.fullmatch(disk):
        fail("The startup disk UUID isn't a valid UUID. Run the Mac script again.")

    board = field("board_id", "board-id")
    if not re.fullmatch(r"[\x21-\x7e]{4,40}", board):
        fail("The board-id has odd characters in it. Run the Mac script again.")
    if not re.fullmatch(r"Mac-[0-9A-Fa-f]{8,16}", board):
        say("WARN", "The board-id isn't in the usual Mac-XXXXXXXX form (patched or OpenCore Macs can show this). It's kept as is.")

    build = field("build", "macOS build")
    if not re.fullmatch(r"\d{2}[A-Z]\d{1,5}[a-z]?", build):
        fail("The macOS build number looks wrong (expected something like 22G513). Run the Mac script again.")

    version = field("version", "macOS version")
    if not re.fullmatch(r"\d+\.\d+(\.\d+)?", version):
        fail("The macOS version looks wrong (expected something like 13.6.4). Run the Mac script again.")

    rom_hex = field("rom", "ROM").lower()
    if not re.fullmatch(r"[0-9a-f]*", rom_hex) or len(rom_hex) % 2:
        fail("The ROM value isn't hex. Run the Mac script again.")
    rom = list(bytes.fromhex(rom_hex))
    if len(rom) != 6:
        fail(f"The ROM is {len(rom)} bytes; a Mac's is 6. Run the Mac script again; if it stays wrong, this Mac's NVRAM can't be read.")

    mlb = field("mlb", "MLB (logic board serial)").upper()
    if not re.fullmatch(r"[A-Z0-9]{12,20}", mlb):
        fail("The MLB isn't 12 to 20 letters and digits. Run the Mac script again.")

    darwin = str(info.get("darwin") or "").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", darwin):
        darwin = darwin_for(version)
        say("WARN", "The Mac didn't report its Darwin version; worked it out from the macOS version.")

    hw = {
        "product_name": model,
        "io_mac_address": mac_bytes,
        "platform_serial_number": serial,
        "platform_uuid": uuid,
        "root_disk_uuid": disk,
        "board_id": board,
        "os_build_num": build,
        "platform_serial_number_enc": [],
        "platform_uuid_enc": [],
        "root_disk_uuid_enc": [],
        "rom": rom,
        "rom_enc": [],
        "mlb": mlb,
        "mlb_enc": [],
    }
    key = {
        "inner": hw,
        "version": version,
        "protocol_version": PROTOCOL_VERSION,
        "device_id": uuid,
        "icloud_ua": ICLOUD_UA.format(darwin),
        "aoskit_version": AOSKIT,
    }

    bridge_dir = os.environ.get("BRIDGE_DIR") or os.path.expanduser("~/imessage-bridge")
    state = os.path.join(bridge_dir, "state")
    os.makedirs(state, mode=0o700, exist_ok=True)
    os.chmod(state, 0o700)
    paths = {n: os.path.join(state, n) for n in ("hardware-key.json", "hardware-key.b64")}
    if not force and any(os.path.exists(p) for p in paths.values()):
        fail("There's already a hardware key in state/. The bridge's Apple registration depends on it, so it isn't replaced. "
             "If this is a fresh setup and you really want the new Mac, run this again with --force.")
    if force and os.path.exists(os.path.join(state, "logged-in")):
        say("WARN", "Replacing the key of a bridge that already logged in. The next login registers a new Mac with Apple.")

    contents = {
        "hardware-key.json": json.dumps(key, indent=1),
        "hardware-key.b64": base64.b64encode(json.dumps(key, separators=(",", ":")).encode()).decode(),
    }
    for name, text in contents.items():
        tmp = paths[name] + ".new"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)  # no trailing newline, like gen-hardware-key.py: login feeds the .b64 verbatim
        os.replace(tmp, paths[name])
        os.chmod(paths[name], 0o600)
    say("OK", "Hardware key saved in state/ (hardware-key.json and hardware-key.b64)")


if __name__ == "__main__":
    main()
