#!/bin/bash
# Heals the bridge's silent receive-stall. The process stays alive and Apple's
# transport keeps delivering frames, but IDS routing dies, so incoming texts
# vanish with no redelivery (your phone still shows "delivered"). The bridge
# logs 'ZERO IDS notifications ... transport alive (inbound frame within Ns)'
# when this happens; its own reconnect does not clear this class of wedge, only
# a full process restart does. start.sh calls this first, so the every-10-min
# task self-heals instead of no-op'ing past a deaf bridge.
#
# Settings (environment variable; default shown):
#   BRIDGE_DIR  where the bridge is installed  ($HOME/imessage-bridge)
BRIDGE_DIR="${BRIDGE_DIR:-$HOME/imessage-bridge}"
log="$BRIDGE_DIR/bridge.log"
pgrep -x matrix-rustpush >/dev/null || exit 0   # not running: start.sh launches it
[ -f "$log" ] || exit 0
strip() { sed 's/\x1b\[[0-9;]*m//g'; }
ts() { grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+[+-][0-9:]+' | head -1; }
body=$(tail -n 400 "$log" | strip)
warn=$(printf '%s\n' "$body" | grep 'ZERO IDS notifications' | tail -1)
[ -z "$warn" ] && exit 0
last_in=$(printf '%s\n' "$body" | grep 'RemoteEventMessage' | tail -1 | ts)
warn_ts=$(printf '%s\n' "$warn" | ts)
frame=$(printf '%s\n' "$warn" | grep -oE 'inbound frame within [0-9]+s' | grep -oE '[0-9]+' | head -1)
e() { date -d "$1" +%s 2>/dev/null; }
w=$(e "$warn_ts"); i=$(e "$last_in"); now=$(date +%s)
[ -z "$w" ] && exit 0
# A warning from before this process started is already handled. Without this
# the same old warning would cycle the bridge every 10 minutes until someone
# texted.
started=$(e "$(ps -o lstart= -p "$(pgrep -x -n matrix-rustpush)")")
[ "${w:-0}" -lt "${started:-0}" ] && exit 0
# Wedged when the warning is newer than our last real inbound, frames are still
# flowing (<180s), and the bridge has had 90s past the warning to self-recover.
if [ "${w:-0}" -gt "${i:-0}" ] && [ "${frame:-9999}" -lt 180 ] && [ $((now - w)) -ge 90 ]; then
  echo "$(date '+%F %T') wedge-guard: receive path stalled, cycling bridge" >> "$log"
  pkill -f matrix-rustpush
  sleep 3
  pgrep -f matrix-rustpush >/dev/null && pkill -9 -f matrix-rustpush
  sleep 1
fi
exit 0
