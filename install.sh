#!/bin/bash
# install.sh — put hike-on/hike-off on your PATH and patch even-terminal.
#
# Safe to re-run; re-run it after every `even-terminal` upgrade (an upgrade
# overwrites the package with a fresh, unpatched session.js).
#
#   ./install.sh                 # installs to ~/.local/bin
#   BIN_DIR=/usr/local/bin ./install.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"

command -v even-terminal >/dev/null 2>&1 || {
    echo "warning: 'even-terminal' not found on PATH."
    echo "  install it with:  npm i -g @evenrealities/even-terminal"
}
command -v tmux >/dev/null 2>&1 || echo "warning: 'tmux' not found (needed by resume-sessions.py)."

mkdir -p "$BIN_DIR"
for script in hike-on hike-off; do
    install -m 0755 "$REPO/bin/$script" "$BIN_DIR/$script"
    echo "installed $BIN_DIR/$script"
done

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "note: $BIN_DIR is not on your PATH — add it to your shell rc." ;;
esac

echo
echo "Applying the even-terminal patch..."
python3 "$REPO/even-terminal/even-terminal-patch.py" || {
    echo "patch step failed — see output above. hike-on/hike-off are still installed."
    exit 1
}

echo
echo "Done. Start with:  hike-on   (stop with:  hike-off)"
