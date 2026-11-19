#!/bin/bash
# Logs the bridge in to the bot Apple ID. Feeds the hardware key, then hands the prompts to you.
# Keep the anisette state: it is the device Apple already 2FA-verified. Wiping it gets new provisioning refused.
#
# Settings (environment variable; default shown):
#   BRIDGE_DIR  where the bridge is installed  ($HOME/imessage-bridge)
# Arguments:
#   --yes-really  log in again even though a login already succeeded once
BRIDGE_DIR="${BRIDGE_DIR:-$HOME/imessage-bridge}"
marker="$BRIDGE_DIR/state/logged-in"
if [ -f "$marker" ] && [ "$1" != "--yes-really" ]; then
  echo "STOP: the bridge already logged in once ($(cat "$marker"))."
  echo "This kills it and logs in from scratch, which can re-register and get the account blocked."
  echo "Use start.sh to restart it. Only run this with --yes-really if the login is truly gone."
  exit 1
fi
pkill -x matrix-rustpush 2>/dev/null
pgrep -f synapse.app.homeserver >/dev/null || (cd "$BRIDGE_DIR/synapse" && "$BRIDGE_DIR/synapse-venv/bin/python" -m synapse.app.homeserver -c homeserver.yaml --daemonize)
cd "$BRIDGE_DIR/data" || exit 1
{ cat "$BRIDGE_DIR/state/hardware-key.b64"; printf '\n\n'; cat; } | "$BRIDGE_DIR/src/matrix-rustpush" login -c config.yaml
status=${PIPESTATUS[1]}
if [ "$status" -eq 0 ]; then
  mkdir -p "$BRIDGE_DIR/state"
  date '+%F %T' > "$marker"
fi
exit "$status"
