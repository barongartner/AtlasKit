#!/bin/bash
# Logs the bridge in to the bot Apple ID. Works in a terminal and when the
# setup app drives it over pipes: every line out is one of
#   STEP: / OK: / WARN: / ERROR: / RESULT: KEY=VALUE / LOG: <bridge output>
#   ASK: <kind> <question>   the app answers with ONE line on stdin
# where <kind> is apple_id, password (secret: never echo it), code (the 2FA
# code), choice (a question with ASK_OPTION: lines before it) or text.
# The hardware key, the Apple ID (BOT_APPLE_ID) and the setup questions after
# the code are answered here, the same way the working install answered them.
#
# Keep the anisette state: it is the device Apple already 2FA-verified. Wiping
# it gets new provisioning refused. Every login attempt counts against the
# Apple ID; never retry in a loop.
#
# Settings (environment variables; defaults shown):
#   BRIDGE_DIR          where the bridge is installed               ($HOME/imessage-bridge)
#   MATRIX_PORT         Synapse port, started if it's down          (8008)
#   BOT_APPLE_ID        the Apple ID to log in with (else ASK: apple_id)
#   BOT_APPLE_PASSWORD  its password (else ASK: password); never passed on to the bridge's environment
#   LOGIN_HANDLE        address to send from, e.g. mailto:x@y.com   (mailto:BOT_APPLE_ID)
# Arguments:
#   --yes-really  log in again even though a login already succeeded once
set -euo pipefail
BRIDGE_DIR="${BRIDGE_DIR:-$HOME/imessage-bridge}"
MATRIX_PORT="${MATRIX_PORT:-8008}"
again=0
case "${1:-}" in
  "") ;;
  --yes-really) again=1 ;;
  *) echo "ERROR: usage: login.sh [--yes-really]"; exit 2 ;;
esac
marker="$BRIDGE_DIR/state/logged-in"
if [ -f "$marker" ] && [ "$again" != 1 ]; then
  echo "ERROR: The bridge already logged in once ($(cat "$marker")). Logging in again kills it and starts from scratch, which can re-register and get the account blocked. Use start.sh to restart it. Only run login.sh --yes-really if the login is truly gone."
  exit 1
fi
BIN="$BRIDGE_DIR/src/matrix-rustpush"
KEY="$BRIDGE_DIR/state/hardware-key.b64"
[ -x "$BIN" ] || { echo "ERROR: The bridge isn't built in $BRIDGE_DIR. Run install.sh first."; exit 1; }
[ -f "$BRIDGE_DIR/data/config.yaml" ] || { echo "ERROR: The bridge has no config yet. Run install.sh bridge-config first."; exit 1; }
[ -s "$KEY" ] || { echo "ERROR: There's no hardware key yet. Run make-key.py with the Mac's info first."; exit 1; }

# Stop THIS install's bridge (the login needs it down). Texts wait on Apple's side.
mine() {
  local pid exe
  for pid in $(pgrep -x matrix-rustpush 2>/dev/null || true); do
    exe=$(readlink "/proc/$pid/exe" 2>/dev/null || true)
    [ "${exe% (deleted)}" = "$BIN" ] && echo "$pid"
  done
  return 0
}
pids=$(mine)
if [ -n "$pids" ]; then
  echo "STEP: Stopping the bridge for the login"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  for _ in $(seq 1 15); do [ -z "$(mine)" ] && break; sleep 1; done
  pids=$(mine)
  # shellcheck disable=SC2086
  [ -z "$pids" ] || kill -9 $pids 2>/dev/null || true
fi
# The login doesn't talk to Matrix, but keep Synapse up like before.
if ! curl -fsS -m 3 "http://127.0.0.1:$MATRIX_PORT/_matrix/client/versions" >/dev/null 2>&1 \
   && [ -x "$BRIDGE_DIR/synapse-venv/bin/python" ] && [ -f "$BRIDGE_DIR/synapse/homeserver.yaml" ]; then
  (cd "$BRIDGE_DIR/synapse" && "$BRIDGE_DIR/synapse-venv/bin/python" -m synapse.app.homeserver -c homeserver.yaml --daemonize) </dev/null >/dev/null 2>&1 \
    || echo "WARN: Synapse didn't start; the login goes ahead without it."
fi

IFS= read -r -d '' DRIVER <<'PY' || true
import getpass, os, re, select, subprocess, sys

binary, keyfile = sys.argv[1], sys.argv[2]
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def out(kind, msg):
    print(f"{kind}: {msg}", flush=True)


with open(keyfile) as f:
    key = "".join(f.read().split())
apple_id = os.environ.get("BOT_APPLE_ID", "").strip()
password = os.environ.pop("BOT_APPLE_PASSWORD", "")
handle_pref = (os.environ.get("LOGIN_HANDLE") or (f"mailto:{apple_id}" if apple_id else "")).strip().lower()
tty = sys.stdin.isatty()

child = subprocess.Popen([binary, "login", "-c", "config.yaml"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=dict(os.environ))
label, options, errors, seen = "", [], [], {}


def send(text):
    try:
        child.stdin.write((text + "\n").encode())
        child.stdin.flush()
    except BrokenPipeError:
        pass


def stop(msg):
    out("ERROR", msg)
    child.kill()
    sys.exit(1)


def ask(kind, question, secret=False):
    out("ASK", f"{kind} {question}")
    if secret and tty:
        return getpass.getpass("")
    line = sys.stdin.readline()
    if not line:
        stop("No answer came in, so the login stopped. Run login.sh again when ready.")
    return line.rstrip("\r\n")


def answer_text(name):
    global apple_id, handle_pref
    low = name.lower()
    if low == "apple id":
        if not apple_id:
            apple_id = ask("apple_id", "Enter the bots' Apple ID (its email address).").strip()
            handle_pref = handle_pref or f"mailto:{apple_id}".lower()
        return apple_id
    if low == "password":
        if password:
            out("STEP", "Sending the Apple ID password")
            return password
        return ask("password", "Enter the password of the bots' Apple ID.", secret=True)
    if "2fa" in low or low.endswith("code"):
        return ask("code", "Enter the 6-digit code Apple just sent (a notification on a trusted device, or a text or voice call to the trusted phone number).")
    if "cloudkit" in low:
        return "no"
    if "video transcoding" in low or "heic" in low:
        return "yes"
    return ask("text", name)


def answer_choice(name, opts):
    low = name.lower()
    if low.startswith("send messages as"):
        plain = [re.sub(r"^\d+\.\s*", "", o).strip().lower() for o in opts]
        if handle_pref in plain:
            return str(plain.index(handle_pref) + 1)
        out("WARN", f"{handle_pref or 'The Apple ID address'} isn't in the bridge's list of addresses; sending from the first one ({plain[0] if plain else '?'}).")
        return "1"
    if low.startswith("contact source"):
        return "1"  # iCloud contacts
    if low.startswith("post facetime call notices"):
        return "yes"
    if low.startswith("enable statuskit notifications"):
        return "no"
    if low.startswith("notification style"):
        return "topic"
    for i, o in enumerate(opts, 1):
        out("ASK_OPTION", f"{i}) {o}")
    return ask("choice", f"{name} (answer with the number)")


def on_line(raw):
    global label, options
    text = ANSI.sub("", raw).rstrip()
    if not text.strip():
        return
    if "(paste, then press Enter on a blank line)" in text:
        out("STEP", "Sending the Mac hardware key")
        send(key)
        send("")
        return
    m = re.match(r"^\s+(\d+)\) (.*)$", text)
    if m and label:
        options.append(m.group(2))
        return
    if text.endswith(":") and not text.startswith("["):
        label, options = text[:-1].strip(), []
    if text.startswith("[!]"):
        errors.append(text[3:].strip())
    t = re.search(r"attempt (\d+)/(\d+) hit Apple's 503 throttle.*?in (\d+)s", text)
    if t:
        out("WARN", f"Apple is throttling logins for this Apple ID. The bridge tries again by itself in "
                    f"{max(1, int(t.group(3)) // 60)} min (attempt {t.group(1)} of {t.group(2)}). Leave it running.")
        return
    if text.startswith("[*] Logging in as"):
        out("STEP", "Starting the Apple login")
    if text.startswith("[+] Login ID"):
        out("STEP", "Apple accepted the login")
    out("LOG", text)


def on_prompt(raw):
    text = ANSI.sub("", raw).strip()
    if not text.endswith(":"):
        return False
    question = f"{label} {text}" if text.startswith("Enter number") else text
    seen[question] = seen.get(question, 0) + 1
    if seen[question] > 3:
        stop(f"The bridge keeps asking '{question}'. Stopping so it doesn't loop.")
    if text.startswith("Enter number"):
        send(answer_choice(label, options))
    else:
        send(answer_text(text[:-1].strip()))
    return True


fd = child.stdout.fileno()
buf = b""
while True:
    ready, _, _ = select.select([fd], [], [], 0.4)
    if ready:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            on_line(line.decode("utf-8", "replace"))
    elif buf and on_prompt(buf.decode("utf-8", "replace")):
        buf = b""
if buf:
    on_line(buf.decode("utf-8", "replace"))
rc = child.wait()
if rc != 0:
    why = errors[-1] if errors else f"the bridge exited with code {rc}"
    text = " ".join(errors).lower()
    if "<html>" in text or "503" in text:
        hint = "Apple is throttling this Apple ID. Wait a few hours, then try once."
    elif "-22405" in text or "-22421" in text:
        hint = "Apple refused after too many attempts. Stop for today and try once tomorrow."
    elif "-22406" in text or "-20101" in text or "incorrect" in text:
        hint = "Check the Apple ID and password, then try once more."
    elif "customer code" in text or "cannot sign in to imessage" in text or "6004" in text:
        hint = "Apple refused iMessage for this Mac's identity. Don't keep retrying; contact Apple Support about iMessage activation."
    elif "endprovisioning" in text:
        hint = "Apple refused to set up a new device. Don't delete anything in the bridge's state; wait and try later."
    elif "2fa" in text or "verification" in text or "security code" in text:
        hint = "The code was wrong or expired. Run login.sh again to get a new one."
    else:
        hint = "The LOG lines above and data/logs/bridge.log have the details. Wait before trying again: every attempt counts against the Apple ID."
    out("ERROR", f"The Apple login failed: {why}. {hint}")
sys.exit(rc)
PY

cd "$BRIDGE_DIR/data"
# An interactive login skips the 6-hour backoff the patched bridge puts on background logins.
export GSA_INTERACTIVE=1
set +e
python3 -u -c "$DRIVER" "$BIN" "$KEY"
status=$?
set -e
unset BOT_APPLE_PASSWORD
if [ "$status" -eq 0 ]; then
  mkdir -p "$BRIDGE_DIR/state"
  date '+%F %T' >"$marker"
  handles=$("$BIN" list-handles 2>/dev/null </dev/null | grep -E '^(mailto|tel):' | tr '[:upper:]' '[:lower:]' | paste -sd, - || true)
  [ -n "$handles" ] && echo "RESULT: IMESSAGE_HANDLES=$handles"
  echo "OK: Logged in to iMessage. Next: start the bridge (start.sh), then run matrix-setup.sh."
fi
exit "$status"
