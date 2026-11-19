# Builds the bridge from source. Settings (environment variable; default shown):
#   BRIDGE_DIR  where the bridge is installed  ($HOME/imessage-bridge)
BRIDGE_DIR="${BRIDGE_DIR:-$HOME/imessage-bridge}"
export PATH=$HOME/.local/go/bin:$HOME/.cargo/bin:$HOME/go/bin:$PATH
export GOTOOLCHAIN=local
cd "$BRIDGE_DIR/src" || exit 1
make build > "$BRIDGE_DIR/build.log" 2>&1
echo "make exit=$?" >> "$BRIDGE_DIR/build.log"
tail -25 "$BRIDGE_DIR/build.log"
