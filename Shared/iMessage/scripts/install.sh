#!/bin/bash
# Installs the iMessage bridge from scratch in WSL (Ubuntu 24.04): system
# packages, Rust and Go, the patched bridge source, the build, a local Synapse
# homeserver and the bridge config. The setup app runs one stage at a time and
# reads the output lines:
#   STEP: <what's happening>   OK: <done>   WARN: <...>   ERROR: <what failed, what to try>
#   RESULT: KEY=VALUE  (values the app saves)    LOG: <raw output worth showing on failure>
# Exit 0 only on success. Every stage is safe to run again: a finished stage
# leaves state/stage-<name>.done and is skipped next time (--force redoes it).
#
# Usage: install.sh <stage> [--force]
#   packages       as root (wsl -u root): apt packages and Apple's root certificate
#   toolchains     Rust and Go for the build, in the user's home
#   source         clone the bridge and rustpush at the pinned commits, apply the patches
#   build          compile the bridge (long: 15 to 60 minutes the first time)
#   synapse        local Matrix homeserver in a Python venv
#   bridge-config  bridge config + appservice registration, Synapse wired to it, bot Matrix user
#   all            every stage after packages, in order (packages must already be done)
#   copy-scripts   just refresh start.sh, wedge-guard.sh and login.sh in BRIDGE_DIR
#
# Settings (environment variables; defaults shown):
#   BRIDGE_DIR          where the bridge lives                ($HOME/imessage-bridge)
#   KIT_IMESSAGE        this kit folder as seen from WSL       (the folder above scripts/)
#   MATRIX_SERVER_NAME  Matrix server name                     (atlas.local)
#   MATRIX_PORT         Synapse port on 127.0.0.1              (8008)
#   BRIDGE_PORT         bridge appservice port on 127.0.0.1    (29332)
#   BOT_USER            Matrix localpart the bots log in as    (atlas)
#   BOT_APPLE_ID        the bots' Apple ID (for the alias list) (empty)
#   BOT_NAMES           bots that get their own alias address  (atlas iris)
#   GO_VERSION RUST_VERSION SYNAPSE_VERSION  toolchain pins    (1.27.2 1.99.1 1.163.0)
#   BUILD_JOBS          parallel compile jobs                  (picked from RAM)
set -euo pipefail
set -E

SELF_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
KIT_IMESSAGE="${KIT_IMESSAGE:-$(dirname "$SELF_DIR")}"
BRIDGE_DIR="${BRIDGE_DIR:-$HOME/imessage-bridge}"
MATRIX_SERVER_NAME="${MATRIX_SERVER_NAME:-atlas.local}"
MATRIX_PORT="${MATRIX_PORT:-8008}"
BRIDGE_PORT="${BRIDGE_PORT:-29332}"
BOT_USER="${BOT_USER:-atlas}"
BOT_APPLE_ID="${BOT_APPLE_ID:-}"
BOT_NAMES="${BOT_NAMES:-atlas iris}"
GO_VERSION="${GO_VERSION:-1.27.2}"
RUST_VERSION="${RUST_VERSION:-1.99.1}"
SYNAPSE_VERSION="${SYNAPSE_VERSION:-1.163.0}"
BUILD_JOBS="${BUILD_JOBS:-}"
export BRIDGE_DIR KIT_IMESSAGE MATRIX_SERVER_NAME MATRIX_PORT BRIDGE_PORT BOT_USER BOT_APPLE_ID BOT_NAMES

BRIDGE_REPO=https://github.com/jasonlaguidice/imessage.git
RUSTPUSH_REPO=https://github.com/OpenBubbles/rustpush.git
APPLE_CA_URL=https://www.apple.com/appleca/AppleIncRootCertificate.cer
APPLE_CA_SHA256=B0B1730ECBC7FF4505142C49F1295E6EDA6BCAED7E2C68C5BE91B5A11001F024
APPLE_CA_FILE=/usr/local/share/ca-certificates/AppleRootCA.crt
ROOT_STATE=/var/lib/imessage-kit

PATCHES="$KIT_IMESSAGE/patches"
STATE="$BRIDGE_DIR/state"
SRC="$BRIDGE_DIR/src"
RP="$SRC/third_party/rustpush-upstream"
APA="$RP/third_party/apple-private-apis"
VENV="$BRIDGE_DIR/synapse-venv"
HS_DIR="$BRIDGE_DIR/synapse"
HS_YAML="$HS_DIR/homeserver.yaml"
DATA="$BRIDGE_DIR/data"
BIN="$SRC/matrix-rustpush"
LOGS="$BRIDGE_DIR/install-logs"

export PATH="$HOME/.local/go/bin:$HOME/.cargo/bin:$HOME/go/bin:$PATH"
export GOTOOLCHAIN=local
export GIT_TERMINAL_PROMPT=0
# rustpush's submodules use git@github.com: URLs; fetch them over https like its Makefile does.
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=url.https://github.com/.insteadOf GIT_CONFIG_VALUE_0=git@github.com:

APT_PACKAGES=(build-essential cmake protobuf-compiler pkg-config git curl wget ca-certificates
  openssl perl sqlite3 procps xz-utils util-linux
  libolm-dev libclang-dev libssl-dev libunicorn-dev libheif-dev zlib1g-dev
  libheif-plugin-libde265 ffmpeg python3 python3-venv python3-dev)
STAGES=(packages toolchains source build synapse bridge-config)

# ------------------------------------------------------------------ output ----
say() { printf '%s\n' "$*"; }
step() { say "STEP: $*"; }
ok() { say "OK: $*"; }
warn() { say "WARN: $*"; }
fail() { say "ERROR: $*"; exit 1; }
CURRENT_STAGE=setup
LAST_LOG=""
TMP=""
on_err() {
  local rc=$? line=$1
  say "ERROR: The $CURRENT_STAGE stage stopped unexpectedly (line $line, exit $rc).${LAST_LOG:+ Details are in $LAST_LOG.} Run the same stage again; it picks up where it left off."
  exit "$rc"
}
trap 'on_err $LINENO' ERR
trap 'if [ -n "$TMP" ]; then rm -rf "$TMP"; fi' EXIT

tmpdir() { [ -n "$TMP" ] || TMP=$(mktemp -d); }

# run_logged LOG CMD...: output goes to LOG; on failure the last lines are shown as LOG: lines.
run_logged() {
  local log=$1; shift
  LAST_LOG=$log
  if ! "$@" >>"$log" 2>&1 </dev/null; then
    tail -n 15 "$log" | sed 's/\r$//; s/^/LOG: /'
    return 1
  fi
}

stage_log() {
  if [ "$(id -u)" = 0 ]; then
    mkdir -p "$ROOT_STATE"; LAST_LOG="$ROOT_STATE/install-$1.log"
  else
    mkdir -p "$LOGS"; LAST_LOG="$LOGS/install-$1.log"
  fi
  printf '\n===== %s %s\n' "$(date '+%F %T')" "$1" >>"$LAST_LOG"
}

need_user() {
  [ "$(id -u)" != 0 ] || fail "Run the $1 stage as the normal WSL user, not root (only 'packages' runs as root)."
}

marker() {
  if [ "$1" = packages ]; then echo "$ROOT_STATE/stage-packages.done"; else echo "$STATE/stage-$1.done"; fi
}
FORCE=0
is_done() { [ "$FORCE" = 0 ] && [ -f "$(marker "$1")" ]; }
mark_done() {
  local m; m=$(marker "$1")
  mkdir -p "$(dirname "$m")"
  date '+%F %T' >"$m"
}

check_kit_files() {
  local f
  for f in BRIDGE_COMMIT RUSTPUSH_COMMIT APPLE_PRIVATE_APIS_COMMIT live-bridge.diff live-rustpush.diff live-apple-private-apis.diff; do
    [ -s "$PATCHES/$f" ] || fail "The kit is missing patches/$f (looked in $PATCHES). Check KIT_IMESSAGE."
  done
}
# kit files may have picked up Windows line endings on the way here
kit_value() {
  [ -f "$PATCHES/$1" ] || fail "The kit is missing patches/$1 (looked in $PATCHES). Check KIT_IMESSAGE."
  tr -d '[:space:]' <"$PATCHES/$1"
}
kit_diff() {  # prints the path of an LF-only copy of a patch
  [ -f "$PATCHES/$1" ] || fail "The kit is missing patches/$1 (looked in $PATCHES). Check KIT_IMESSAGE."
  tmpdir
  sed 's/\r$//' "$PATCHES/$1" >"$TMP/$1"
  echo "$TMP/$1"
}

copy_scripts() {
  local f dest
  for f in start.sh wedge-guard.sh scripts/login.sh; do
    [ -f "$KIT_IMESSAGE/$f" ] || fail "The kit is missing $f (looked in $KIT_IMESSAGE). Check KIT_IMESSAGE."
    dest="$BRIDGE_DIR/$(basename "$f")"
    sed 's/\r$//' "$KIT_IMESSAGE/$f" >"$dest.new"
    chmod 755 "$dest.new"
    mv -f "$dest.new" "$dest"
  done
  ok "Copied start.sh, wedge-guard.sh and login.sh into $BRIDGE_DIR"
}

packages_installed() {
  local p
  for p in "${APT_PACKAGES[@]}"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || return 1
  done
  [ -f "$APPLE_CA_FILE" ]
}

# --------------------------------------------------------------- synapse ----
hs_pid_ours() {  # prints the pid of THIS install's Synapse, if it runs
  # The pid file path comes from this install's homeserver.yaml, so a live
  # process named in it is ours (a daemonized Synapse moves its cwd to /).
  local pid
  [ -f "$HS_DIR/homeserver.pid" ] || return 1
  pid=$(tr -d '[:space:]' <"$HS_DIR/homeserver.pid")
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
  tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -q 'synapse.app.homeserver' || return 1
  echo "$pid"
}

bridge_running() {  # this install's bridge, not any other on the machine
  local pid exe
  for pid in $(pgrep -x matrix-rustpush 2>/dev/null || true); do
    exe=$(readlink "/proc/$pid/exe" 2>/dev/null || true)
    [ "${exe% (deleted)}" = "$BIN" ] && return 0
  done
  return 1
}
hs_answers() { curl -fsS -m 3 "http://127.0.0.1:$MATRIX_PORT/_matrix/client/versions" >/dev/null 2>&1; }

start_synapse() {
  local i
  if hs_pid_ours >/dev/null; then
    for i in $(seq 1 30); do hs_answers && return 0; sleep 1; done
    fail "Synapse is running but doesn't answer on port $MATRIX_PORT. Check $HS_DIR/homeserver.log."
  fi
  if hs_answers; then
    fail "Something else already answers on port $MATRIX_PORT (another Matrix server?). Stop it or set MATRIX_PORT to a free port, then run this stage with --force."
  fi
  step "Starting Synapse on 127.0.0.1:$MATRIX_PORT"
  # 9>&-: the daemon must not inherit the install lock
  if ! (cd "$HS_DIR" && "$VENV/bin/python" -m synapse.app.homeserver -c homeserver.yaml --daemonize) >>"$LAST_LOG" 2>&1 </dev/null 9>&-; then
    tail -n 15 "$LAST_LOG" | sed 's/^/LOG: /'
    fail "Synapse didn't start. The reason is in $LAST_LOG and $HS_DIR/homeserver.log."
  fi
  for i in $(seq 1 60); do
    if hs_answers; then ok "Synapse is up on 127.0.0.1:$MATRIX_PORT"; return 0; fi
    sleep 1
  done
  tail -n 15 "$HS_DIR/homeserver.log" 2>/dev/null | sed 's/^/LOG: /'
  fail "Synapse started but never answered on port $MATRIX_PORT. Check $HS_DIR/homeserver.log."
}

restart_synapse() {
  local pid i
  if pid=$(hs_pid_ours); then
    step "Restarting Synapse so it loads the bridge registration"
    kill "$pid" 2>/dev/null || true
    for i in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    sleep 1
  fi
  start_synapse
}

# ------------------------------------------------------------------ stages ----
stage_packages() {
  [ "$(id -u)" = 0 ] || fail "The packages stage has to run as root: wsl -d <distro> -u root -- bash install.sh packages"
  stage_log packages
  command -v apt-get >/dev/null || fail "This distro has no apt-get. The kit needs Ubuntu 24.04."
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    [ "${VERSION_ID:-}" = 24.04 ] || warn "This is ${PRETTY_NAME:-an unknown distro}, not Ubuntu 24.04. Package names may differ."
  fi
  export DEBIAN_FRONTEND=noninteractive
  local apt_opts=(-y -q -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
  step "Updating the package lists"
  run_logged "$LAST_LOG" apt-get "${apt_opts[@]}" update \
    || fail "apt-get update failed. Check the internet connection in WSL and run this stage again."
  step "Installing build tools and libraries (a few minutes)"
  run_logged "$LAST_LOG" apt-get "${apt_opts[@]}" install --no-install-recommends "${APT_PACKAGES[@]}" \
    || fail "Installing packages failed. Details are in $LAST_LOG. Run this stage again; if it keeps failing, run 'apt-get -f install' as root."
  ok "System packages installed"

  # Apple's identity servers chain to Apple Root CA, which Ubuntu doesn't trust by default.
  local have=""
  [ -f "$APPLE_CA_FILE" ] && have=$(openssl x509 -in "$APPLE_CA_FILE" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2 | tr -d ':')
  if [ "$have" != "$APPLE_CA_SHA256" ]; then
    step "Adding Apple's root certificate"
    tmpdir
    curl -fsSL -m 60 "$APPLE_CA_URL" -o "$TMP/apple.cer" \
      || fail "Couldn't download Apple's root certificate. Check the internet connection and run this stage again."
    have=$(openssl x509 -inform DER -in "$TMP/apple.cer" -noout -fingerprint -sha256 | cut -d= -f2 | tr -d ':')
    [ "$have" = "$APPLE_CA_SHA256" ] || fail "The certificate downloaded from apple.com isn't Apple Root CA (fingerprint mismatch). Something is intercepting the connection; don't continue on this network."
    openssl x509 -inform DER -in "$TMP/apple.cer" -out "$APPLE_CA_FILE"
    run_logged "$LAST_LOG" update-ca-certificates || fail "update-ca-certificates failed. Details are in $LAST_LOG."
  fi
  ok "Apple Root CA is trusted"
  mark_done packages
}

stage_toolchains() {
  need_user toolchains
  stage_log toolchains
  tmpdir
  # Rust, pinned to what the working install builds with
  if [ -x "$HOME/.cargo/bin/rustup" ]; then
    if ! rustup toolchain list 2>/dev/null | grep -q "^$RUST_VERSION-"; then
      step "Installing Rust $RUST_VERSION"
      run_logged "$LAST_LOG" rustup toolchain install "$RUST_VERSION" --profile minimal \
        || fail "Installing Rust $RUST_VERSION failed. Check the internet connection and run this stage again."
    fi
    run_logged "$LAST_LOG" rustup default "$RUST_VERSION" || fail "Couldn't make Rust $RUST_VERSION the default."
  else
    step "Installing Rust $RUST_VERSION with rustup"
    curl --proto '=https' --tlsv1.2 -fsSL -m 120 https://sh.rustup.rs -o "$TMP/rustup-init.sh" \
      || fail "Couldn't download the Rust installer. Check the internet connection and run this stage again."
    run_logged "$LAST_LOG" sh "$TMP/rustup-init.sh" -y --profile minimal --default-toolchain "$RUST_VERSION" \
      || fail "The Rust installer failed. Details are in $LAST_LOG."
  fi
  case "$(rustc --version 2>/dev/null)" in
    "rustc $RUST_VERSION"*) ok "Rust $RUST_VERSION ready" ;;
    *) fail "rustc isn't $RUST_VERSION after installing (got: $(rustc --version 2>&1 | head -1)). Check for a RUSTUP_TOOLCHAIN override." ;;
  esac

  # Go, into ~/.local/go
  local arch file sha
  case "$(uname -m)" in
    x86_64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    *) fail "Unsupported CPU type $(uname -m) for Go." ;;
  esac
  if [ -f "$HOME/.local/go/VERSION" ] && [ "$(head -n1 "$HOME/.local/go/VERSION")" = "go$GO_VERSION" ]; then
    ok "Go $GO_VERSION already installed"
  else
    step "Installing Go $GO_VERSION"
    file="go$GO_VERSION.linux-$arch.tar.gz"
    sha=$(curl -fsSL -m 60 'https://go.dev/dl/?mode=json&include=all' | python3 -c '
import json, sys
name = sys.argv[1]
for rel in json.load(sys.stdin):
    for f in rel.get("files", []):
        if f.get("filename") == name:
            print(f["sha256"]); sys.exit(0)
sys.exit(1)' "$file") || fail "Couldn't look up the checksum for $file on go.dev. Check the internet connection and run this stage again."
    curl -fsSL -m 600 "https://go.dev/dl/$file" -o "$TMP/$file" \
      || fail "Couldn't download $file. Check the internet connection and run this stage again."
    echo "$sha  $TMP/$file" | sha256sum -c --quiet - >/dev/null 2>&1 \
      || fail "The Go download is corrupt (checksum mismatch). Run this stage again."
    rm -rf "$HOME/.local/go.new"
    mkdir -p "$HOME/.local/go.new"
    tar -C "$HOME/.local/go.new" -xzf "$TMP/$file"
    rm -rf "$HOME/.local/go"
    mv "$HOME/.local/go.new/go" "$HOME/.local/go"
    rmdir "$HOME/.local/go.new"
  fi
  case "$(go version 2>/dev/null)" in
    *"go$GO_VERSION "*) ok "Go $GO_VERSION ready" ;;
    *) fail "go isn't $GO_VERSION after installing (got: $(go version 2>&1 | head -1))." ;;
  esac
  mark_done toolchains
}

# apply_diff DIR PATCHFILE LABEL: applies once; a second run sees it's already in.
diff_files() { grep '^diff --git a/' "$1" | sed 's#^diff --git a/\(.*\) b/.*$#\1#'; }
apply_diff() {
  local dir=$1 diff=$2 label=$3 f
  if git -C "$dir" apply --check --reverse "$diff" >/dev/null 2>&1; then
    ok "The $label patch is already applied"
    return 0
  fi
  if [ "$FORCE" = 1 ]; then
    step "Resetting the files the $label patch touches"
    while IFS= read -r f; do
      if git -C "$dir" ls-files --error-unmatch -- "$f" >/dev/null 2>&1; then
        git -C "$dir" checkout HEAD -- "$f"
      else
        rm -f "$dir/$f"
      fi
    done < <(diff_files "$diff")
  fi
  if git -C "$dir" apply --check "$diff" >>"$LAST_LOG" 2>&1; then
    git -C "$dir" apply "$diff"
    ok "Applied the $label patch"
  else
    tail -n 10 "$LAST_LOG" | sed 's/^/LOG: /'
    fail "The $label patch doesn't apply in $dir: those files were changed by something else. Run 'install.sh source --force' to reset them and patch again."
  fi
}

# clone_at URL DEST COMMIT LABEL: resumable clone of one repo pinned to a commit.
clone_at() {
  local url=$1 dest=$2 commit=$3 label=$4 part="$2.partial"
  if [ ! -d "$dest/.git" ]; then
    if [ -e "$dest" ]; then
      [ "$FORCE" = 1 ] || fail "$dest exists but isn't a git checkout. Move it away or run 'install.sh source --force'."
      rm -rf "$dest"
    fi
    if [ ! -d "$part/.git" ]; then
      rm -rf "$part"
      step "Downloading the $label source"
      run_logged "$LAST_LOG" git clone "$url" "$part" \
        || fail "Couldn't download the $label source from GitHub. Check the internet connection and run this stage again."
    fi
    git -C "$part" cat-file -e "$commit^{commit}" 2>/dev/null || run_logged "$LAST_LOG" git -C "$part" fetch --all --tags \
      || { rm -rf "$part"; fail "Couldn't fetch $label commit $commit. Run this stage again."; }
    run_logged "$LAST_LOG" git -C "$part" -c advice.detachedHead=false checkout --detach "$commit" \
      || { rm -rf "$part"; fail "Couldn't check out $label at $commit. Details are in $LAST_LOG. Run this stage again."; }
    mv "$part" "$dest"
    return 0
  fi
  [ "$(git -C "$dest" rev-parse HEAD)" = "$commit" ] && return 0
  if [ -n "$(git -C "$dest" status --porcelain --untracked-files=no)" ] && [ "$FORCE" != 1 ]; then
    fail "$dest is at a different commit and has local changes. Run 'install.sh source --force' to reset it to the kit's commit."
  fi
  git -C "$dest" cat-file -e "$commit^{commit}" 2>/dev/null || run_logged "$LAST_LOG" git -C "$dest" fetch --all --tags \
    || fail "Couldn't fetch $label commit $commit."
  run_logged "$LAST_LOG" git -C "$dest" -c advice.detachedHead=false checkout --force --detach "$commit" \
    || fail "Couldn't check out $label at $commit."
}

stage_source() {
  need_user source
  stage_log source
  case "$BRIDGE_DIR" in
    *" "*) fail "BRIDGE_DIR can't contain spaces; the bridge's build breaks on them. Use a path like \$HOME/imessage-bridge." ;;
  esac
  [ "$BRIDGE_DIR" = "$HOME/imessage-bridge" ] || warn "BRIDGE_DIR isn't \$HOME/imessage-bridge. Some patched features (login backoff, the shared profile name and photo, the iCloud pause switch) look for their files in \$HOME/imessage-bridge/state no matter what, so they read and write there."
  command -v git >/dev/null && command -v make >/dev/null && command -v perl >/dev/null \
    || fail "git, make or perl is missing. Run the packages stage as root first."
  local bridge_commit rp_commit apa_commit d_bridge d_rp d_apa pin subs_done="$STATE/rustpush-submodules.done"
  check_kit_files
  tmpdir
  bridge_commit=$(kit_value BRIDGE_COMMIT)
  rp_commit=$(kit_value RUSTPUSH_COMMIT)
  apa_commit=$(kit_value APPLE_PRIVATE_APIS_COMMIT)
  d_bridge=$(kit_diff live-bridge.diff)
  d_rp=$(kit_diff live-rustpush.diff)
  d_apa=$(kit_diff live-apple-private-apis.diff)
  mkdir -p "$BRIDGE_DIR" "$DATA" "$HS_DIR"
  mkdir -p -m 700 "$STATE"

  # 1. the bridge at its pinned commit, with our patch
  clone_at "$BRIDGE_REPO" "$SRC" "$bridge_commit" bridge
  ok "Bridge source at ${bridge_commit:0:7}"
  apply_diff "$SRC" "$d_bridge" bridge

  # 2. rustpush, cloned the way the bridge's Makefile does it, but patched BEFORE
  #    `make` runs: the snapshot already contains the Makefile's own source edits,
  #    which then report "already satisfied" instead of clashing with the patch.
  pin=$(tr -d '[:space:]' <"$SRC/third_party/rustpush-upstream.sha" 2>/dev/null || true)
  [ "$pin" = "$rp_commit" ] || fail "The bridge pins rustpush at ${pin:-nothing}, but the kit's patch is for $rp_commit. The kit's BRIDGE_COMMIT and RUSTPUSH_COMMIT don't belong together."
  [ -d "$RP/.git" ] || rm -f "$subs_done"
  clone_at "$RUSTPUSH_REPO" "$RP" "$rp_commit" rustpush
  # Only once per clone: the Makefile later swaps the open-absinthe submodule
  # for a plain copy, and a second 'submodule update' would trip over it.
  if [ ! -f "$subs_done" ]; then
    step "Downloading rustpush's submodules"
    run_logged "$LAST_LOG" git -C "$RP" submodule sync --recursive || fail "git submodule sync failed in rustpush."
    run_logged "$LAST_LOG" git -C "$RP" submodule update --init --recursive \
      || fail "Couldn't download rustpush's submodules. Check the internet connection and run this stage again."
    date '+%F %T' >"$subs_done"
  fi
  if [ "$(git -C "$APA" rev-parse HEAD 2>/dev/null)" != "$apa_commit" ]; then
    warn "apple-private-apis isn't at the kit's commit; checking out ${apa_commit:0:7}"
    git -C "$APA" cat-file -e "$apa_commit^{commit}" 2>/dev/null || run_logged "$LAST_LOG" git -C "$APA" fetch --all \
      || fail "Couldn't fetch apple-private-apis commit $apa_commit."
    run_logged "$LAST_LOG" git -C "$APA" -c advice.detachedHead=false checkout --force --detach "$apa_commit" \
      || fail "Couldn't check out apple-private-apis at $apa_commit."
  fi
  ok "rustpush source at ${rp_commit:0:7}"
  apply_diff "$RP" "$d_rp" rustpush
  apply_diff "$APA" "$d_apa" apple-private-apis

  # 3. the Makefile's own prep: FairPlay cert stubs, the open-absinthe overlay,
  #    and its source edits (all already in the patch, so it only checks them).
  step "Preparing the rustpush tree"
  run_logged "$LAST_LOG" make -C "$SRC" ensure-rustpush-source \
    || fail "Preparing rustpush failed. Details are in $LAST_LOG."
  git -C "$RP" apply --check --reverse "$d_rp" >/dev/null 2>&1 \
    && git -C "$APA" apply --check --reverse "$d_apa" >/dev/null 2>&1 \
    || fail "The rustpush patches no longer match after the Makefile's prep. Run 'install.sh source --force'."
  ok "Source ready in $SRC"
  copy_scripts
  mark_done source
}

progress_filter() {  # reads make output, writes it to $1, prints a STEP now and then
  local log=$1 n=0 line phase=""
  while IFS= read -r line; do
    printf '%s\n' "$line" >>"$log"
    case "$line" in
      *"Checking Linux build dependencies"*) say "STEP: Checking build dependencies" ;;
      *"Compiling "*)
        n=$((n + 1))
        if [ "$phase" != rust ]; then phase=rust; say "STEP: Compiling the Rust library (the long part)"; fi
        [ $((n % 25)) = 0 ] && say "STEP: Compiling the Rust library: $n packages done"
        ;;
      *"go build"*)
        if [ "$phase" != go ]; then phase=go; say "STEP: Compiling the Go bridge"; fi
        ;;
      *"Built matrix-rustpush"*) say "STEP: Finishing up" ;;
    esac
  done
  return 0
}

stage_build() {
  need_user build
  stage_log build
  [ -d "$SRC/.git" ] && [ -d "$RP/certs/fairplay" ] || fail "The source isn't ready. Run the source stage first."
  command -v go >/dev/null && command -v cargo >/dev/null || fail "Go or Rust is missing. Run the toolchains stage first."
  local avail_kb mem_kb log="$BRIDGE_DIR/build.log" rc
  avail_kb=$(df -Pk "$BRIDGE_DIR" | awk 'NR==2{print $4}')
  if [ "${avail_kb:-0}" -lt $((8 * 1024 * 1024)) ]; then
    fail "Only $((avail_kb / 1024 / 1024)) GB free in WSL; the build needs about 10 GB. Free some space and run this stage again."
  elif [ "$avail_kb" -lt $((12 * 1024 * 1024)) ]; then
    warn "Only $((avail_kb / 1024 / 1024)) GB free in WSL; the build needs about 10 GB."
  fi
  if [ -z "$BUILD_JOBS" ]; then
    mem_kb=$(awk '/^MemTotal/{print $2}' /proc/meminfo)
    if [ "$mem_kb" -lt $((7 * 1024 * 1024)) ]; then BUILD_JOBS=2
    elif [ "$mem_kb" -lt $((11 * 1024 * 1024)) ]; then BUILD_JOBS=4
    fi
  fi
  [ -n "$BUILD_JOBS" ] && export CARGO_BUILD_JOBS="$BUILD_JOBS"
  # The Makefile's dependency check calls sudo for anything missing. Nobody can
  # type a password here, so a stub makes that fail at once instead of hanging.
  mkdir -p "$BRIDGE_DIR/.kit-bin"
  printf '#!/bin/sh\necho "sudo needed: $*" >&2\nexit 1\n' >"$BRIDGE_DIR/.kit-bin/sudo"
  chmod 755 "$BRIDGE_DIR/.kit-bin/sudo"

  step "Building the bridge. The first build takes 15 to 60 minutes; later ones are quicker"
  LAST_LOG=$log
  printf '===== %s make build\n' "$(date '+%F %T')" >"$log"
  if (cd "$SRC" && PATH="$BRIDGE_DIR/.kit-bin:$PATH" make build </dev/null 2>&1) | progress_filter "$log"; then rc=0; else rc=$?; fi
  echo "make exit=$rc" >>"$log"
  if [ "$rc" != 0 ] || [ ! -x "$BIN" ]; then
    tail -n 20 "$log" | sed 's/\x1b\[[0-9;]*m//g; s/^/LOG: /'
    if grep -q 'sudo needed' "$log"; then
      fail "The build wanted to install system packages or Apple's certificate. Run the packages stage as root again, check WSL can reach identity.ess.apple.com, then run build again."
    elif grep -qiE 'No space left on device' "$log"; then
      fail "WSL ran out of disk space during the build. Free about 10 GB and run build again."
    elif grep -qE 'signal: 9|SIGKILL|Killed' "$log"; then
      fail "The build ran out of memory. Run build again with BUILD_JOBS=2, or give WSL more memory in .wslconfig."
    elif grep -qiE 'Could not resolve host|Failed to connect|network unreachable|spurious network error' "$log"; then
      fail "The build couldn't download its dependencies. Check the internet connection and run build again."
    fi
    fail "The build failed. Details are in $log. Run build again; it continues where it stopped."
  fi
  local ver
  ver=$("$BIN" -v 2>/dev/null | head -n1 || true)
  ok "Built $BIN${ver:+ ($ver)}"
  mark_done build
}

stage_synapse() {
  need_user synapse
  stage_log synapse
  command -v python3 >/dev/null || fail "python3 is missing. Run the packages stage as root first."
  mkdir -p "$HS_DIR"
  mkdir -p -m 700 "$STATE"
  if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -c 'import sys' 2>/dev/null; then
    step "Creating the Python environment for Synapse"
    rm -rf "$VENV"
    run_logged "$LAST_LOG" python3 -m venv "$VENV" || fail "Couldn't create a Python venv. Run the packages stage as root (it installs python3-venv)."
  fi
  if [ "$FORCE" = 1 ] || ! "$VENV/bin/python" -c "import importlib.metadata as m, sys; sys.exit(m.version('matrix-synapse') != '$SYNAPSE_VERSION')" 2>/dev/null; then
    step "Installing Synapse $SYNAPSE_VERSION (a few minutes)"
    run_logged "$LAST_LOG" "$VENV/bin/python" -m pip install -q --upgrade pip \
      || fail "Couldn't update pip. Check the internet connection and run this stage again."
    run_logged "$LAST_LOG" "$VENV/bin/python" -m pip install -q "matrix-synapse==$SYNAPSE_VERSION" \
      || fail "Installing Synapse failed. Details are in $LAST_LOG."
  fi
  ok "Synapse $SYNAPSE_VERSION installed"

  if [ ! -f "$HS_YAML" ]; then
    step "Generating the homeserver config for $MATRIX_SERVER_NAME (fresh secrets)"
    if ! (cd "$HS_DIR" && umask 077 && "$VENV/bin/python" -m synapse.app.homeserver \
          --server-name "$MATRIX_SERVER_NAME" --config-path "$HS_YAML" --data-directory "$HS_DIR" \
          --generate-config --report-stats=no) >>"$LAST_LOG" 2>&1 </dev/null; then
      tail -n 15 "$LAST_LOG" | sed 's/^/LOG: /'
      fail "Generating the Synapse config failed. Details are in $LAST_LOG."
    fi
  fi
  step "Applying the homeserver settings"
  "$VENV/bin/python" - "$HS_YAML" "$MATRIX_SERVER_NAME" "$MATRIX_PORT" <<'PY' || exit 1
import os, sys, yaml
path, name, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    with open(path) as f:
        c = yaml.safe_load(f)
except Exception as e:
    print(f"ERROR: Couldn't read {path} ({e}). Move it away and run the synapse stage again.")
    sys.exit(1)
if c.get("server_name") != name:
    print(f"ERROR: Synapse here was set up for server name {c.get('server_name')!r}, not {name!r}. "
          "A server name can't change once Synapse has data. Use the old MATRIX_SERVER_NAME, "
          "or move the synapse folder away and run this stage again.")
    sys.exit(1)
# Same choices as the working install: local only, SQLite, no stats, no link
# previews, stored media cleared after a week.
c["listeners"] = [{
    "port": port, "bind_addresses": ["127.0.0.1"], "type": "http", "tls": False,
    "x_forwarded": True, "resources": [{"names": ["client", "federation"], "compress": False}],
}]
c["report_stats"] = False
c["media_retention"] = {"local_media_lifetime": "7d", "remote_media_lifetime": "7d"}
c["url_preview_enabled"] = False  # previews made inbound links 502 and stall the bridge queue
c["url_preview_ip_range_blacklist"] = ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                       "100.64.0.0/10", "169.254.0.0/16", "::1/128", "fe80::/10", "fc00::/7"]
c["suppress_key_server_warning"] = True
c.setdefault("app_service_config_files", [])
tmp = path + ".new"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    yaml.safe_dump(c, f, sort_keys=False, default_flow_style=False)
os.replace(tmp, path)
PY
  chmod 600 "$HS_YAML"
  ok "Homeserver $MATRIX_SERVER_NAME set up on 127.0.0.1:$MATRIX_PORT"
  start_synapse
  mark_done synapse
}

alias_results() {
  [ -n "$BOT_APPLE_ID" ] || { warn "BOT_APPLE_ID isn't set, so there is no per-bot alias list yet."; return 0; }
  case "$BOT_APPLE_ID" in
    *@*.*) ;;
    *) warn "BOT_APPLE_ID doesn't look like an email address; per-bot aliases need an email Apple ID."; return 0 ;;
  esac
  local local_part=${BOT_APPLE_ID%@*} domain=${BOT_APPLE_ID#*@} name list=""
  local_part=${local_part%%+*}
  for name in $BOT_NAMES; do
    list="$list${list:+,}$local_part+$name@$domain"
  done
  case "$domain" in
    gmail.com|googlemail.com|outlook.com|hotmail.com|icloud.com|me.com|fastmail.com) ;;
    *) warn "Check that $domain delivers plus addresses (like $local_part+${BOT_NAMES%% *}@$domain) to the main inbox; Apple has to send a code to each one." ;;
  esac
  say "RESULT: APPLE_ALIASES=$list"
}

stage_bridge_config() {
  need_user bridge-config
  stage_log bridge-config
  [ -x "$BIN" ] || fail "The bridge isn't built yet. Run the build stage first."
  [ -f "$HS_YAML" ] && [ -x "$VENV/bin/python" ] || fail "Synapse isn't set up yet. Run the synapse stage first."
  mkdir -p "$DATA"
  mkdir -p -m 700 "$STATE"
  if [ ! -f "$DATA/config.yaml" ]; then
    step "Writing the bridge's example config"
    if ! (cd "$DATA" && "$BIN" -c config.yaml -e) >>"$LAST_LOG" 2>&1 </dev/null; then
      tail -n 15 "$LAST_LOG" | sed 's/^/LOG: /'
      fail "The bridge couldn't write its example config. Details are in $LAST_LOG."
    fi
  fi
  step "Applying the bridge settings"
  "$VENV/bin/python" - "$DATA/config.yaml" "$MATRIX_SERVER_NAME" "$MATRIX_PORT" "$BRIDGE_PORT" "$BOT_USER" "$DATA" <<'PY' || exit 1
import os, sys, yaml
path, name, hs_port, as_port, bot_user, data = sys.argv[1:7]
try:
    with open(path) as f:
        c = yaml.safe_load(f)
except Exception as e:
    print(f"ERROR: Couldn't read {path} ({e}). Move it away and run bridge-config again.")
    sys.exit(1)
def section(key):
    c.setdefault(key, {})
    if c[key] is None:
        c[key] = {}
    return c[key]
hs = section("homeserver")
hs["address"] = f"http://127.0.0.1:{hs_port}"
hs["domain"] = name
hs["software"] = "standard"
asv = section("appservice")
asv["address"] = f"http://localhost:{as_port}"
asv["hostname"] = "127.0.0.1"
asv["port"] = int(as_port)
db = section("database")
db["type"] = "sqlite3-fk-wal"
db["uri"] = f"file:{os.path.join(data, 'mautrix-imessage.db')}?_txlock=immediate"
br = section("bridge")
# Only the bots' Matrix user. The CLI login picks the first admin it finds,
# so there must be exactly one.
br["permissions"] = {f"@{bot_user}:{name}": "admin"}
net = section("network")
# Changed from the defaults on the working install. The login asks most of
# these again and stores the answers; login.sh answers the same way.
net["video_transcoding"] = True
net["heic_conversion"] = True
net["cloudkit_backfill"] = False
net["disable_facetime"] = False
net["statuskit_share_on_startup"] = False
net["statuskit_notifications"] = False
tmp = path + ".new"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    yaml.safe_dump(c, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
os.replace(tmp, path)
PY
  if [ ! -f "$DATA/registration.yaml" ]; then
    step "Generating the appservice registration"
    if ! (cd "$DATA" && "$BIN" -c config.yaml -g -r registration.yaml) >>"$LAST_LOG" 2>&1 </dev/null; then
      tail -n 15 "$LAST_LOG" | sed 's/^/LOG: /'
      fail "The bridge couldn't generate its registration. Details are in $LAST_LOG."
    fi
  else
    ok "Keeping the existing registration (its tokens match the config)"
  fi
  chmod 600 "$DATA/config.yaml" "$DATA/registration.yaml"
  grep -q "localhost:$BRIDGE_PORT" "$DATA/registration.yaml" \
    || warn "registration.yaml points at a different port than BRIDGE_PORT=$BRIDGE_PORT. If you changed BRIDGE_PORT, move registration.yaml away and run bridge-config --force."

  step "Telling Synapse about the bridge"
  "$VENV/bin/python" - "$HS_YAML" "$DATA/registration.yaml" <<'PY' || fail "Couldn't add the registration to $HS_YAML."
import os, sys, yaml
path, reg = sys.argv[1], sys.argv[2]
with open(path) as f:
    c = yaml.safe_load(f)
files = c.get("app_service_config_files") or []
if reg not in files:
    files.append(reg)
c["app_service_config_files"] = files
tmp = path + ".new"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    yaml.safe_dump(c, f, sort_keys=False, default_flow_style=False)
os.replace(tmp, path)
PY
  restart_synapse
  if (exec 3<>"/dev/tcp/127.0.0.1/$BRIDGE_PORT") 2>/dev/null && ! bridge_running; then
    warn "Something else already listens on port $BRIDGE_PORT. The bridge won't start until it's free; or set BRIDGE_PORT, move data/registration.yaml away and run bridge-config --force."
  fi
  bash "$SELF_DIR/matrix-setup.sh" --user-only 9>&- || exit 1
  copy_scripts
  alias_results
  ok "Bridge configured. Next: the Mac identity (make-key.py), then login.sh"
  mark_done bridge-config
}

# ------------------------------------------------------------------- main ----
usage() {
  say "ERROR: usage: install.sh <packages|toolchains|source|build|synapse|bridge-config|all|copy-scripts> [--force]"
  exit 2
}
STAGE=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,32p' "${BASH_SOURCE[0]}"; exit 0 ;;
    -*) usage ;;
    *) [ -z "$STAGE" ] || usage; STAGE=$arg ;;
  esac
done
[ -n "$STAGE" ] || usage

case "$MATRIX_PORT$BRIDGE_PORT" in *[!0-9]*) fail "MATRIX_PORT and BRIDGE_PORT must be numbers." ;; esac
for p in "$MATRIX_PORT" "$BRIDGE_PORT"; do
  case "$p" in 8010|8011) fail "Port $p is taken by the bridge's own FaceTime control ports (8010, 8011). Pick another." ;; esac
done
[ "$MATRIX_PORT" != "$BRIDGE_PORT" ] || fail "MATRIX_PORT and BRIDGE_PORT can't be the same port."
case "$BOT_USER" in *[!a-z0-9._=-]*|"") fail "BOT_USER must be a lowercase Matrix localpart (letters, digits, . _ = -)." ;; esac

# one run at a time
if [ "$(id -u)" = 0 ]; then
  mkdir -p "$ROOT_STATE"; exec 9>"$ROOT_STATE/install.lock"
else
  mkdir -p "$BRIDGE_DIR"; exec 9>"$BRIDGE_DIR/.install.lock"
fi
flock -n 9 || fail "Another install step is already running. Wait for it to finish."

run_stage() {
  CURRENT_STAGE=$1
  if is_done "$1"; then
    ok "The $1 stage is already done (install.sh $1 --force redoes it)"
    return 0
  fi
  case "$1" in
    packages) stage_packages ;;
    toolchains) stage_toolchains ;;
    source) stage_source ;;
    build) stage_build ;;
    synapse) stage_synapse ;;
    bridge-config) stage_bridge_config ;;
  esac
  ok "Stage $1 finished"
}

case "$STAGE" in
  packages|toolchains|source|build|synapse|bridge-config) run_stage "$STAGE" ;;
  copy-scripts) need_user copy-scripts; copy_scripts ;;
  all)
    need_user all
    if ! is_done packages; then
      packages_installed || fail "System packages are missing. Run 'install.sh packages' as root first (wsl -u root)."
      ok "System packages are already installed"
    fi
    for s in "${STAGES[@]}"; do
      [ "$s" = packages ] && continue
      run_stage "$s"
    done
    ok "All install stages finished"
    ;;
  *) usage ;;
esac
exit 0
