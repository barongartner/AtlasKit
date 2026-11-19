#!/usr/bin/env python3
# Builds the bridge's "external key" (base64 JSON) for a generated Intel Mac.
# The bridge computes the five _enc IOKit values itself on Linux, so only plaintext is needed.
# Serial and MLB come from OpenCore's macserial:  ./macserial -m MacBookAir8,1 -n 1
# Usage: gen-hardware-key.py SERIAL MLB   (writes to $BRIDGE_DIR/state, never into a repo)
# BRIDGE_DIR is an environment variable, default ~/imessage-bridge.
import base64, json, os, secrets, sys, uuid

force = "--overwrite-live-key" in sys.argv
args = [a for a in sys.argv[1:] if a != "--overwrite-live-key"]
if len(args) != 2:
    sys.exit("usage: gen-hardware-key.py SERIAL MLB [--overwrite-live-key]")
serial, mlb = args

state = os.path.join(os.environ.get("BRIDGE_DIR") or os.path.expanduser("~/imessage-bridge"), "state")
os.makedirs(state, mode=0o700, exist_ok=True)
# A bridge that is set up is registered with Apple under the key already in
# state/ (normally a real Mac's identity). This script writes a made-up
# MacBookAir8,1 over it, and nothing backs state/ up: the registration would be
# gone, and Apple has refused generated Macs before. Only a from-scratch setup
# on a machine with no key should ever get past this.
if not force and any(os.path.exists(os.path.join(state, n)) for n in ("hardware-key.json", "hardware-key.b64")):
    sys.exit("STOP: state/ already has a hardware key, and the bridge's Apple registration depends on it.\n"
             "This would replace it with a made-up Mac. If that is really what you want, copy state/\n"
             "somewhere safe first, then add --overwrite-live-key.")
platform_uuid = str(uuid.uuid4()).upper()

hw = {
    "product_name": "MacBookAir8,1",
    "io_mac_address": [0xa4, 0x83, 0xe7] + list(secrets.token_bytes(3)),  # Apple OUI
    "platform_serial_number": serial,
    "platform_uuid": platform_uuid,
    "root_disk_uuid": str(uuid.uuid4()).upper(),
    "board_id": "Mac-827FAC58A8FDFA22",
    "os_build_num": "22G513",
    "platform_serial_number_enc": [], "platform_uuid_enc": [], "root_disk_uuid_enc": [],
    "rom": list(secrets.token_bytes(6)), "rom_enc": [],
    "mlb": mlb, "mlb_enc": [],
}
cfg = {
    "inner": hw, "version": "13.6.4", "protocol_version": 1660,
    "device_id": platform_uuid,
    "icloud_ua": "com.apple.iCloudHelper/282 CFNetwork/1568.100.1 Darwin/22.6.0",
    "aoskit_version": "com.apple.AOSKit/282 (com.apple.accountsd/113)",
}

for name, data in (("hardware-key.json", json.dumps(cfg, indent=1)),
                   ("hardware-key.b64", base64.b64encode(json.dumps(cfg, separators=(",", ":")).encode()).decode())):
    path = os.path.join(state, name)
    with open(path, "w") as f:
        f.write(data)
    os.chmod(path, 0o600)
    print("wrote", path)
