#!/bin/bash
# Starts Synapse and the iMessage bridge if they aren't running. Safe to run any time.
# Appends to bridge.log so history survives restarts.
#
# Settings (environment variables the Control Center passes; defaults shown):
#   BRIDGE_DIR      where the bridge is installed      ($HOME/imessage-bridge)
#   KEEPALIVE_TASK  the Windows task holding WSL open  (Atlas WSL Keepalive)
BRIDGE_DIR="${BRIDGE_DIR:-$HOME/imessage-bridge}"
KEEPALIVE_TASK="${KEEPALIVE_TASK:-Atlas WSL Keepalive}"

# The keepalive task (wsl.exe -- sleep infinity) is what holds the VM open
# between runs of this script, and it only starts at logon. When it dies,
# every run boots the VM, starts Synapse and the bridge, and loses them again
# seconds later. Bring it back from here: Windows ignores the run request if
# the task is somehow already going.
if ! pgrep -f '^sleep infinity$' >/dev/null; then
  schtasks=$(command -v schtasks.exe || echo /mnt/c/Windows/System32/schtasks.exe)
  "$schtasks" /run /tn "$KEEPALIVE_TASK" </dev/null >/dev/null 2>&1 \
    && echo "$(date '+%F %T') start.sh: the WSL keepalive was gone, started it again" >> "$BRIDGE_DIR/bridge.log"
fi

cd "$BRIDGE_DIR/synapse" || exit 1
pgrep -f synapse.app.homeserver >/dev/null || "$BRIDGE_DIR/synapse-venv/bin/python" -m synapse.app.homeserver -c homeserver.yaml --daemonize
cd "$BRIDGE_DIR/data" || exit 1
export RUST_LOG="warn,rustpush=warn,rustpush::ids::link=info,rustpush::ids::identity_manager=debug,rustpushgo=info,open_absinthe=info"
BRIDGE_DIR="$BRIDGE_DIR" bash "$BRIDGE_DIR/wedge-guard.sh"  # heal a silently-wedged receive path before (re)launch
pgrep -x matrix-rustpush >/dev/null || nohup "$BRIDGE_DIR/src/matrix-rustpush" -c config.yaml >> "$BRIDGE_DIR/bridge.log" 2>&1 &
sleep 2
pgrep -f synapse.app.homeserver >/dev/null && echo "synapse up" || echo "synapse DOWN"
pgrep -x matrix-rustpush >/dev/null && echo "bridge up" || echo "bridge DOWN"
