#!/bin/bash
# Sets up the Matrix side the bots use: the bot Matrix user (created if
# missing, password kept in state/ with mode 600), an access token, and the
# bridge management room (the DM with the bridge bot where the bots send
# "start-chat <number>" to open new iMessage chats).
#
# Run it after a successful Apple login (login.sh) with Synapse and the bridge
# running (start.sh). install.sh's bridge-config stage runs it with
# --user-only before the login, which only makes the user.
#
# Output lines: STEP: / OK: / WARN: / ERROR: / RESULT: KEY=VALUE. This is the
# one script that prints the Matrix token (RESULT: MATRIX_TOKEN=...), for the
# app to store in the bots' .env. Exit 0 only on success. Safe to run again:
# it reuses the user, the saved token and the existing management room.
#
# Settings (environment variables; defaults shown):
#   BRIDGE_DIR          where the bridge lives                ($HOME/imessage-bridge)
#   MATRIX_SERVER_NAME  Matrix server name                     (atlas.local)
#   MATRIX_PORT         Synapse port on 127.0.0.1              (8008)
#   BOT_USER            Matrix localpart the bots log in as    (atlas)
#   BOT_APPLE_ID        the bots' Apple ID, for the per-bot alias results (empty)
#   BOT_NAMES           bots that get their own alias address  (atlas iris)
# Arguments:
#   --user-only   only create the Matrix user (no token printed, no room)
set -euo pipefail
BRIDGE_DIR="${BRIDGE_DIR:-$HOME/imessage-bridge}"
MATRIX_SERVER_NAME="${MATRIX_SERVER_NAME:-atlas.local}"
MATRIX_PORT="${MATRIX_PORT:-8008}"
BOT_USER="${BOT_USER:-atlas}"
BOT_APPLE_ID="${BOT_APPLE_ID:-}"
BOT_NAMES="${BOT_NAMES:-atlas iris}"
mode=full
case "${1:-}" in
  "") ;;
  --user-only) mode=user ;;
  *) echo "ERROR: usage: matrix-setup.sh [--user-only]"; exit 2 ;;
esac
PY="$BRIDGE_DIR/synapse-venv/bin/python"
[ -x "$PY" ] || { echo "ERROR: Synapse isn't installed in $BRIDGE_DIR. Run install.sh synapse first."; exit 1; }

exec "$PY" - "$mode" "$BRIDGE_DIR" "$MATRIX_SERVER_NAME" "$MATRIX_PORT" "$BOT_USER" "$BOT_APPLE_ID" "$BOT_NAMES" <<'PYEOF'
import hashlib, hmac, json, os, re, secrets, sqlite3, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request

import yaml

mode, bridge_dir, server, port, bot_user, apple_id, bot_names = sys.argv[1:8]
base = f"http://127.0.0.1:{port}"
mxid = f"@{bot_user}:{server}"
state = os.path.join(bridge_dir, "state")
hs_dir = os.path.join(bridge_dir, "synapse")
hs_yaml = os.path.join(hs_dir, "homeserver.yaml")
data = os.path.join(bridge_dir, "data")
binary = os.path.join(bridge_dir, "src", "matrix-rustpush")
pw_file = os.path.join(state, f"matrix-{bot_user}-password")
token_file = os.path.join(state, f"matrix-{bot_user}-token")


def say(kind, msg):
    print(f"{kind}: {msg}", flush=True)


def fail(msg):
    say("ERROR", msg)
    sys.exit(1)


def api(method, path, body=None, token=None, timeout=20):
    """Returns (status, json). Network trouble is status 0."""
    req = urllib.request.Request(base + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, payload, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError, ValueError):
        return 0, {}


def write_secret(path, value):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def read_secret(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def q(s):
    return urllib.parse.quote(s, safe="")


# ---------------------------------------------------------------- synapse ----
def synapse_up():
    st, _ = api("GET", "/_matrix/client/versions", timeout=5)
    return st == 200


if not synapse_up():
    if not os.path.exists(hs_yaml):
        fail(f"Synapse isn't set up in {hs_dir}. Run install.sh synapse first.")
    say("STEP", f"Starting Synapse on 127.0.0.1:{port}")
    log = open(os.path.join(hs_dir, "start.log"), "a")
    subprocess.run([os.path.join(bridge_dir, "synapse-venv", "bin", "python"), "-m", "synapse.app.homeserver",
                    "-c", "homeserver.yaml", "--daemonize"], cwd=hs_dir, stdin=subprocess.DEVNULL,
                   stdout=log, stderr=log, check=False)
    for _ in range(60):
        if synapse_up():
            break
        time.sleep(1)
    else:
        fail(f"Synapse doesn't answer on port {port}. Check {hs_dir}/homeserver.log, or run start.sh.")

st, key = api("GET", "/_matrix/key/v2/server")
if st == 200 and key.get("server_name") and key["server_name"] != server:
    fail(f"The Matrix server on port {port} is {key['server_name']}, not {server}. Check MATRIX_PORT and MATRIX_SERVER_NAME.")

# ------------------------------------------------------------------- user ----
st, avail = api("GET", "/_matrix/client/v3/register/available?username=" + q(bot_user))
if st == 200 and avail.get("available"):
    say("STEP", f"Creating the Matrix user {mxid}")
    password = read_secret(pw_file)
    if not password:
        password = secrets.token_urlsafe(32)
        write_secret(pw_file, password)  # saved before registering, so a crash can't lose it
    try:
        with open(hs_yaml) as f:
            hs = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        fail(f"Couldn't read {hs_yaml} ({e}).")
    shared = hs.get("registration_shared_secret")
    if not shared and hs.get("registration_shared_secret_path"):
        shared = read_secret(hs["registration_shared_secret_path"])
    if not shared:
        fail("Synapse has no registration_shared_secret, so the user can't be created. Re-run install.sh synapse --force.")
    st, n = api("GET", "/_synapse/admin/v1/register")
    if st != 200 or "nonce" not in n:
        fail(f"Synapse refused the registration request (HTTP {st}). Check {hs_dir}/homeserver.log.")
    # Synapse's shared-secret registration: HMAC-SHA1 over nonce, user, password, "admin"
    mac = hmac.new(shared.encode(), digestmod=hashlib.sha1)
    mac.update(b"\x00".join([n["nonce"].encode(), bot_user.encode(), password.encode(), b"admin"]))
    st, r = api("POST", "/_synapse/admin/v1/register", {
        "nonce": n["nonce"], "username": bot_user, "password": password,
        "admin": True, "mac": mac.hexdigest()})
    if st != 200:
        fail(f"Creating {mxid} failed (HTTP {st}, {r.get('errcode', 'no error code')}). Check {hs_dir}/homeserver.log.")
    say("OK", f"Created the Matrix user {mxid}")
elif st == 400 and avail.get("errcode") == "M_USER_IN_USE":
    say("OK", f"The Matrix user {mxid} already exists")
elif st == 0:
    fail(f"Synapse stopped answering on port {port}. Run start.sh, then this again.")
else:
    fail(f"Couldn't check whether {mxid} exists (HTTP {st}, {avail.get('errcode', 'no error code')}).")

say("RESULT", f"MATRIX_USER={mxid}")
say("RESULT", f"MATRIX_URL={base}")
if mode == "user":
    sys.exit(0)

# ------------------------------------------------------------------ token ----
token = read_secret(token_file)
if token:
    st, who = api("GET", "/_matrix/client/v3/account/whoami", token=token)
    if st != 200 or who.get("user_id") != mxid:
        token = ""
if not token:
    password = read_secret(pw_file)
    if not password:
        fail(f"{mxid} exists, but its password isn't in {pw_file}. Put the password in that file "
             "(one line, mode 600) and run this again.")
    say("STEP", f"Logging in as {mxid}")
    st, r = api("POST", "/_matrix/client/v3/login", {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": bot_user},
        "password": password,
        "initial_device_display_name": "Bot crew"})
    if st != 200 or not r.get("access_token"):
        fail(f"Logging in as {mxid} failed (HTTP {st}, {r.get('errcode', 'no error code')}). "
             f"If the password in {pw_file} is wrong, fix it and run this again.")
    token = r["access_token"]
    write_secret(token_file, token)
say("OK", f"Have an access token for {mxid}")

# -------------------------------------------------------- management room ----
try:
    with open(os.path.join(data, "config.yaml")) as f:
        cfg = yaml.safe_load(f)
except (OSError, yaml.YAMLError) as e:
    fail(f"Couldn't read the bridge config in {data} ({e}). Run install.sh bridge-config first.")
bot_mxid = f"@{cfg['appservice']['bot']['username']}:{server}"
prefix = (cfg.get("bridge") or {}).get("command_prefix") or "!im"
db_path = os.path.join(data, "mautrix-imessage.db")


def db_mgmt_room():
    if not os.path.exists(db_path):
        return ""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute('SELECT management_room FROM "user" WHERE mxid = ?', (mxid,)).fetchone()
        finally:
            con.close()
        return (row[0] or "") if row else ""
    except sqlite3.Error:
        return ""


def joined_rooms():
    st, r = api("GET", "/_matrix/client/v3/joined_rooms", token=token)
    return set(r.get("joined_rooms", [])) if st == 200 else set()


def members(room):
    st, r = api("GET", f"/_matrix/client/v3/rooms/{q(room)}/joined_members", token=token)
    return set((r.get("joined") or {}).keys()) if st == 200 else set()


def usable(room):
    return room and members(room) == {mxid, bot_mxid}


say("STEP", "Finding the bridge management room")
room = db_mgmt_room()
if room:
    if room not in joined_rooms():
        api("POST", f"/_matrix/client/v3/rooms/{q(room)}/join", {}, token=token)
    if not usable(room):
        room = ""

if not room:
    st, r = api("POST", "/_matrix/client/v3/createRoom", {
        "preset": "trusted_private_chat", "is_direct": True, "invite": [bot_mxid]}, token=token)
    if st != 200 or not r.get("room_id"):
        fail(f"Couldn't create a room with the bridge bot (HTTP {st}, {r.get('errcode', 'no error code')}).")
    room = r["room_id"]
    say("STEP", "Waiting for the bridge bot to join")
    for _ in range(45):
        if bot_mxid in members(room):
            break
        time.sleep(2)
    else:
        fail("The bridge bot never joined. Make sure the bridge is running (start.sh) and logged in, then run this again.")
    for i in range(20):
        if db_mgmt_room() == room:
            break
        if i == 5:  # the bridge already had a management room it can't use: point it here
            api("PUT", f"/_matrix/client/v3/rooms/{q(room)}/send/m.room.message/kit{int(time.time() * 1000)}",
                {"msgtype": "m.text", "body": f"{prefix} set-management-room"}, token=token)
        time.sleep(1.5)
    else:
        fail("The bridge didn't take the new room as its management room. Run this again; "
             "if it keeps failing, check data/logs/bridge.log.")
say("OK", "Management room ready")

# ------------------------------------------------ handles and per-bot aliases ----
handles = []
try:
    out = subprocess.run([binary, "list-handles"], cwd=data, stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=120)
    handles = [h.strip().lower() for h in out.stdout.splitlines()
               if re.match(r"^\s*(mailto|tel):\S+\s*$", h)]
except (OSError, subprocess.SubprocessError):
    pass
if handles:
    say("RESULT", "IMESSAGE_HANDLES=" + ",".join(handles))
else:
    say("WARN", "Couldn't list the bridge's registered addresses (is the Apple login done?).")

if apple_id and "@" in apple_id:
    local, domain = apple_id.strip().lower().rsplit("@", 1)
    local = local.split("+", 1)[0]
    main = f"mailto:{local}@{domain}"
    names = bot_names.split()
    say("RESULT", f"IMESSAGE_ALSO={main}")
    if names:
        say("RESULT", f"IMESSAGE_DEFAULT_BOT={names[0]}")
    missing = []
    for name in names:
        via = f"mailto:{local}+{name.lower()}@{domain}"
        say("RESULT", f"IMESSAGE_VIA_{re.sub(r'[^A-Z0-9]', '_', name.upper())}={via}")
        if handles and via not in handles:
            missing.append(via[len("mailto:"):])
    if missing:
        say("WARN", "Not registered with iMessage yet: " + ", ".join(missing) + ". Add each one to the "
            "Apple ID as an extra email address (Apple account settings, Email and Phone Numbers), "
            "confirm the code Apple emails, then restart the bridge and run this again.")
elif apple_id:
    say("WARN", "BOT_APPLE_ID isn't an email address, so there are no per-bot alias addresses.")

say("RESULT", f"IMESSAGE_MGMT_ROOM={room}")
say("RESULT", f"MATRIX_TOKEN={token}")
say("OK", "Matrix setup done")
PYEOF
