#!/usr/bin/env bash
# iterm2-harness installer.
#
# By default this creates a symlink under iTerm2's AutoLaunch folder pointing
# at iterm2-harness.py in this repo, so iTerm2 launches the service on startup.
#
# Usage:
#   ./install.sh                       # Symlink install (default), source = this dir
#   ./install.sh --source <path>       # Specify source .py path (used by brew formula)
#   ./install.sh --copy                # Copy instead of symlink
#   ./install.sh --target <dir>        # Custom iTerm2 Scripts target directory
#   ./install.sh --uninstall           # Uninstall (remove the link/copy)
#
# Homebrew formula example:
#   bin.install "iterm2-harness.py"
#   (bin/"iterm2-harness-install").write <<~SH
#     #!/bin/bash
#     exec "#{prefix}/install.sh" --source "#{bin}/iterm2-harness.py" "$@"
#   SH
# or invoke directly during post_install:
#   system "#{prefix}/install.sh", "--source", "#{bin}/iterm2-harness.py"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_SOURCE="$SCRIPT_DIR/iterm2-harness.py"
DEFAULT_TARGET_DIR="$HOME/Library/Application Support/iTerm2/Scripts/AutoLaunch"
LINK_NAME="iterm2-harness.py"

SOURCE=""
TARGET_DIR=""
MODE="link"     # link | copy
ACTION="install"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    --target) TARGET_DIR="$2"; shift 2 ;;
    --copy) MODE="copy"; shift ;;
    --link) MODE="link"; shift ;;
    --uninstall) ACTION="uninstall"; shift ;;
    -h|--help)
      sed -n '2,22p' "$0"
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

SOURCE="${SOURCE:-$DEFAULT_SOURCE}"
TARGET_DIR="${TARGET_DIR:-$DEFAULT_TARGET_DIR}"
TARGET="$TARGET_DIR/$LINK_NAME"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

if [[ "$ACTION" == "uninstall" ]]; then
  if [[ -L "$TARGET" || -f "$TARGET" ]]; then
    info "Removing $TARGET"
    rm -f "$TARGET"
  else
    warn "Not found: $TARGET, skipping"
  fi
  exit 0
fi

# Install flow.
[[ -f "$SOURCE" ]] || err "Source file not found: $SOURCE"

info "Source: $SOURCE"
info "Target: $TARGET"
info "Mode:   $MODE"

mkdir -p "$TARGET_DIR"

if [[ -e "$TARGET" || -L "$TARGET" ]]; then
  # Idempotent: if the existing symlink already points at the same source, we're done.
  if [[ -L "$TARGET" && "$(readlink "$TARGET")" == "$SOURCE" && "$MODE" == "link" ]]; then
    info "Symlink already in place, skipping"
    exit 0
  fi
  warn "Existing $TARGET will be overwritten"
  rm -f "$TARGET"
fi

case "$MODE" in
  link) ln -s "$SOURCE" "$TARGET" ;;
  copy) cp "$SOURCE" "$TARGET" ;;
esac

# Make the source executable for convenient CLI debugging (iTerm2 doesn't require it).
chmod +x "$SOURCE" 2>/dev/null || true

info "Installed. iTerm2 will auto-launch the service on its next start."
info "Run now: open iTerm2 > Scripts menu > AutoLaunch > $LINK_NAME"
info "Config:  $(dirname "$SOURCE")/config.json"
info "Data:    ~/.iterm2-harness/  (tokens.json, logs/)"
