#!/bin/bash
# install.sh — put the `hike` command on your PATH and patch even-terminal.
#
# Only `hike` lands on your PATH; its helpers (the on/off/status scripts and the
# free/resume Python tools) go to a private libexec dir that `hike` dispatches to.
#
# Safe to re-run; re-run it after every `even-terminal` upgrade (an upgrade
# overwrites the package with a fresh, unpatched session.js).
#
#   ./install.sh                 # installs to ~/.local/bin
#   BIN_DIR=/usr/local/bin ./install.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
# libexec sits beside bin (e.g. ~/.local/bin -> ~/.local/libexec/hike), matching
# the `$SELF/../libexec/hike` lookup in bin/hike.
LIBEXEC="$(dirname "$BIN_DIR")/libexec/hike"

command -v even-terminal >/dev/null 2>&1 || {
    echo "warning: 'even-terminal' not found on PATH."
    echo "  install it with:  npm i -g @evenrealities/even-terminal"
}
command -v tmux >/dev/null 2>&1 || echo "warning: 'tmux' not found (needed by 'hike resume')."

mkdir -p "$BIN_DIR" "$LIBEXEC"

install -m 0755 "$REPO/bin/hike" "$BIN_DIR/hike"
echo "installed $BIN_DIR/hike"

# Helpers `hike` calls — kept off PATH so only `hike` is user-facing.
for helper in hike-on hike-off hike-status; do
    install -m 0755 "$REPO/bin/$helper" "$LIBEXEC/$helper"
done
for tool in free-sessions.py resume-sessions.py; do
    install -m 0755 "$REPO/even-terminal/$tool" "$LIBEXEC/$tool"
done
echo "installed helpers to $LIBEXEC"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "note: $BIN_DIR is not on your PATH — add it to your shell rc." ;;
esac

echo
echo "Applying the even-terminal patch..."
python3 "$REPO/even-terminal/even-terminal-patch.py" || {
    echo "patch step failed — see output above. 'hike' is still installed."
    exit 1
}

echo
echo "Done. Start with:  hike on   (stop with:  hike off · check with:  hike status)"
