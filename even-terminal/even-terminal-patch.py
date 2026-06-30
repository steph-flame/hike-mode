#!/usr/bin/env python3
"""Patch even-terminal so glasses sessions inherit the resumed session's real
settings instead of even-terminal's hardcoded defaults:

  * model           -> the resumed session's actual model (was: claude-opus-4-6)
  * permissionMode  -> the resumed session's actual mode  (was: acceptEdits)
  * cwd             -> the dir whose Claude project bucket holds the session's
                       transcript, so `claude --resume` can actually find it (was:
                       whatever the app supplied, which --resume often couldn't locate)
  * maxTurns        -> effectively unbounded               (was: 50)

It also injects a one-time "you're on a hike" preamble in front of the FIRST turn
of each resumed session, so Claude switches into HUD rendering (the hud-profile
skill) without being asked. even-terminal only runs while the hike bridge is up, so
"bridge running" == "on a hike"; the preamble is prepended to the user's first
message for a given session id and never repeated. Set EVEN_HIKE_NOTE="" to disable.

For a NEW glasses session (no session to resume) the model is left unset so the
Agent SDK applies its own default, and the permission mode falls back to "acceptEdits".
A new session is not greeted (it has no session id yet on its first turn).

Idempotent: re-run after every `even-terminal` upgrade (an upgrade overwrites
the package with a fresh, unpatched session.js). Fails loud if even-terminal's
internals changed so the anchors no longer match — that's the signal to
re-inspect, same as the firewall/model re-break pattern.

Usage:
  python3 even-terminal-patch.py           # apply (default)
  python3 even-terminal-patch.py --check    # report patched/unpatched, no writes
  python3 even-terminal-patch.py --restore  # restore the most recent backup

Runtime env overrides the patch adds (read by even-terminal, not by this script):
  EVEN_MODEL            force the model for ALL sessions (e.g. claude-opus-4-8)
  EVEN_PERMISSION_MODE  force the permission mode for ALL sessions
  EVEN_MAX_TURNS        force the per-prompt turn cap (default 100000)
  EVEN_HIKE_NOTE        override the first-turn hike preamble; "" disables greeting
"""

from __future__ import annotations

# ruff: noqa: E501 — embedded JS anchors/replacements must match even-terminal's
# source lines verbatim, so several string literals legitimately exceed 100 cols.
import shutil
import subprocess
import sys
import time
from pathlib import Path

MARKER = "__EVEN_PATCH__"

# Extended imports + a single transcript reader that returns both the last
# model and the last permission mode in one backward pass (early-exit once both
# are found) so a large .jsonl is read at most once per turn.
IMPORT_ANCHOR = 'import { existsSync } from "node:fs";'
IMPORT_REPLACEMENT = """import { existsSync, readFileSync as _ppReadFile, readdirSync as _ppReaddir } from "node:fs";
import { join as _ppJoin } from "node:path";
import { homedir as _ppHome } from "node:os";
// __EVEN_PATCH__ read a resumed session's real model, permission mode + resume cwd from its transcript
function _ppSessionMeta(sessionId) {
    const out = { model: undefined, permissionMode: undefined, cwd: undefined };
    if (!sessionId) return out;
    // Claude Code names a transcript's project bucket by replacing every
    // non-alphanumeric char of the creation cwd with "-". Mirror that so we can
    // tell which recorded cwd owns the bucket this transcript actually lives in.
    const _ppEnc = (s) => s.replace(/[^a-zA-Z0-9]/g, "-");
    try {
        const base = _ppJoin(_ppHome(), ".claude", "projects");
        for (const dir of _ppReaddir(base)) {
            const p = _ppJoin(base, dir, `${sessionId}.jsonl`);
            if (!existsSync(p)) continue;
            const lines = _ppReadFile(p, "utf8").trimEnd().split("\\n");
            for (let i = lines.length - 1; i >= 0 && (!out.model || !out.permissionMode || !out.cwd); i--) {
                let o;
                try { o = JSON.parse(lines[i]); } catch { continue; }
                if (!out.permissionMode && o.type === "permission-mode" && o.permissionMode)
                    out.permissionMode = o.permissionMode;
                // require a real "claude-*" id: Claude Code also writes synthetic
                // assistant turns with model "<synthetic>", which must not leak into the query
                if (!out.model && o.type === "assistant" && o.message && typeof o.message.model === "string" && o.message.model.startsWith("claude-"))
                    out.model = o.message.model.replace(/\\[.*$/, "");
                // resume cwd must be the recorded cwd whose bucket holds THIS transcript,
                // or `claude --resume` reports "no conversation found". A session that
                // cd'd into a subdir records that subdir as its later cwd, but the subdir's
                // bucket has no transcript — so match the bucket, don't take the last cwd.
                if (!out.cwd && typeof o.cwd === "string" && _ppEnc(o.cwd) === dir && existsSync(o.cwd))
                    out.cwd = o.cwd;
            }
            break;
        }
    } catch {}
    return out;
}
// __EVEN_PATCH__ the one-time "you're on a hike" preamble prepended to the first turn
// of each resumed session. EVEN_HIKE_NOTE overrides it; setting it to "" disables greeting.
function _ppHikeNote() {
    const env = process.env.EVEN_HIKE_NOTE;
    if (env !== undefined) return env;
    return "[hike-mode] You're being supervised from Even G2 glasses while away from the keyboard (a hike, a walk): replies render on a tiny heads-up display, are read aloud, and are answered by voice or a ring tap. Use the hud-profile skill for the rest of this glasses session \\u2014 verdict first, ~one screen, no wide tables, and keep questions tap-sized (recommendation first). This preamble was auto-injected by hike-mode; the human's actual message follows.";
}"""

# (anchor, replacement) pairs applied after the import block. Each anchor MUST
# appear exactly once.
EDITS = [
    # Resolve the resumed session's settings once, just before the query call, and
    # prepend the hike preamble to the first turn of each resumed session (tracked on
    # the instance so it fires exactly once per session id per bridge run).
    (
        "        const q = query({",
        "        const _ppMeta = _ppSessionMeta(this.sessionId); // __EVEN_PATCH__\n"
        "        let _ppPrompt = prompt; // __EVEN_PATCH__\n"
        "        if (typeof prompt === \"string\" && this.sessionId) { // __EVEN_PATCH__\n"
        "            this._ppGreeted = this._ppGreeted || new Set();\n"
        "            if (!this._ppGreeted.has(this.sessionId)) {\n"
        "                this._ppGreeted.add(this.sessionId);\n"
        "                const _ppNote = _ppHikeNote();\n"
        "                if (_ppNote) _ppPrompt = _ppNote + \"\\n\\n---\\n\\n\" + prompt;\n"
        "            }\n"
        "        }\n"
        "        const q = query({",
    ),
    (
        "            prompt,",
        "            prompt: _ppPrompt, // __EVEN_PATCH__",
    ),
    (
        "                cwd: this.lockedCwd,",
        "                cwd: _ppMeta.cwd || this.lockedCwd, // __EVEN_PATCH__",
    ),
    (
        '                model: "claude-opus-4-6",',
        "                model: process.env.EVEN_MODEL || _ppMeta.model, // __EVEN_PATCH__",
    ),
    (
        '                permissionMode: "acceptEdits",',
        '                permissionMode: process.env.EVEN_PERMISSION_MODE || _ppMeta.permissionMode || "acceptEdits", // __EVEN_PATCH__',
    ),
    (
        "                maxTurns: 50,",
        "                maxTurns: Number(process.env.EVEN_MAX_TURNS) || 100000, // __EVEN_PATCH__",
    ),
]


def locate_session_js() -> Path:
    try:
        root = subprocess.run(
            ["npm", "root", "-g"], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
        candidate = Path(root) / "@evenrealities/even-terminal/dist/claude/session.js"
        if candidate.exists():
            return candidate
    except (subprocess.SubprocessError, OSError):
        pass  # npm missing or errored — fall back to the known Homebrew path
    fallback = Path(
        "/opt/homebrew/lib/node_modules/@evenrealities/even-terminal/dist/claude/session.js"
    )
    if fallback.exists():
        return fallback
    sys.exit("ERROR: could not locate even-terminal's session.js (is it installed?)")


def node_syntax_ok(path: Path) -> bool:
    try:
        subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, check=True)
        return True
    except Exception as e:
        print(f"  node --check failed: {getattr(e, 'stderr', e)}")
        return False


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "--apply"
    if mode not in ("--apply", "--check", "--restore"):
        sys.exit(f"ERROR: unknown mode {mode!r}. Use --apply (default), --check, or --restore.")
    path = locate_session_js()
    text = path.read_text()

    if mode == "--check":
        print(f"{path}\n  {'PATCHED' if MARKER in text else 'UNPATCHED'}")
        return

    if mode == "--restore":
        backups = sorted(path.parent.glob("session.js.bak-*"))
        if not backups:
            sys.exit("ERROR: no backup found. Restore with: npm i -g @evenrealities/even-terminal")
        shutil.copy2(backups[-1], path)
        print(f"Restored {path.name} from {backups[-1].name}")
        return

    if MARKER in text:
        print(f"Already patched: {path}\n(re-run after an even-terminal upgrade.)")
        return

    # Verify every anchor matches exactly once BEFORE touching anything.
    for anchor, _ in [(IMPORT_ANCHOR, None), *EDITS]:
        n = text.count(anchor)
        if n != 1:
            sys.exit(
                f"ERROR: anchor matched {n} times (expected 1):\n  {anchor!r}\n"
                "even-terminal's internals changed — re-inspect session.js before patching."
            )

    backup = path.with_name(f"session.js.bak-{time.strftime('%Y%m%dT%H%M%S')}")
    shutil.copy2(path, backup)

    patched = text.replace(IMPORT_ANCHOR, IMPORT_REPLACEMENT)
    for anchor, repl in EDITS:
        patched = patched.replace(anchor, repl)
    path.write_text(patched)

    if not node_syntax_ok(path):
        shutil.copy2(backup, path)
        sys.exit("ERROR: patched file failed node --check; reverted. No changes applied.")

    print(f"Patched: {path}")
    print(f"Backup:  {backup.name}")
    print("Restart even-terminal (hike-off && hike-on, or restart the server) to take effect.")


if __name__ == "__main__":
    main()
