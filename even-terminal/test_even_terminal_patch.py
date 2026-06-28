"""Tests for `utilities/even-terminal/even-terminal-patch.py`.

The patch rewrites even-terminal's closed-source `session.js` via verbatim string
anchors, so the two things that can silently break are:

  1. The anchor / replace machinery (apply, idempotency, fail-loud on a moved anchor,
     backup + node --check auto-revert, --check / --restore).
  2. The embedded `_ppSessionMeta` JS that reads a resumed session's real model,
     permission mode + resume cwd from its transcript — the `<synthetic>` skip,
     the `[1m]`-suffix strip, the last-wins permission-mode round-trip, and the
     bucket-matching cwd capture (the cwd whose Claude project bucket holds this
     transcript, so `--resume` can find it — not the subdir the session wandered into).

(1) is exercised end-to-end through `main()` against a temp fixture (locate_session_js
monkeypatched). (2) is exercised by running the *actual* injected JS under node against a
fixture transcript — the same runtime even-terminal uses — so the regex/guard behaviour is
validated, not re-implemented in Python.

The script lives next to this file (not on the package path), so it's loaded by file path.
Tests that need a JS engine are skipped when `node` is unavailable (e.g. a CI image without
it); the pure anchor-verification paths still run everywhere.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _bucket(path: str) -> str:
    """Mirror Claude Code's project-bucket naming: non-alphanumeric -> '-'."""
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


_SCRIPT = Path(__file__).resolve().parent / "even-terminal-patch.py"
_spec = importlib.util.spec_from_file_location("even_terminal_patch", _SCRIPT)
assert _spec and _spec.loader
patch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(patch)

_HAS_NODE = shutil.which("node") is not None
_needs_node = pytest.mark.skipif(not _HAS_NODE, reason="node not available")

# A minimal session.js that is valid JS and contains each anchor exactly once, at the
# same indentation even-terminal uses. `query`/`existsSync` need not be defined —
# `node --check` validates syntax only, not references.
FIXTURE_SESSION_JS = """\
import { existsSync } from "node:fs";

class Session {
    constructor(id) {
        this.sessionId = id;
    }
    run() {
        const q = query({
                model: "claude-opus-4-6",
                permissionMode: "acceptEdits",
                maxTurns: 50,
                cwd: this.lockedCwd,
        });
        return q;
    }
}
"""


def _write_fixture(tmp_path: Path, text: str = FIXTURE_SESSION_JS) -> Path:
    path = tmp_path / "session.js"
    path.write_text(text)
    return path


def _run_main(monkeypatch, path: Path, *args: str) -> None:
    """Drive the script's `main()` against `path` with the given CLI args."""
    monkeypatch.setattr(patch, "locate_session_js", lambda: path)
    monkeypatch.setattr(sys, "argv", ["even-terminal-patch.py", *args])
    patch.main()


# --------------------------------------------------------------------------------------
# Anchor / replace machinery
# --------------------------------------------------------------------------------------


@_needs_node
def test_apply_rewrites_the_hardcoded_settings(monkeypatch, tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    _run_main(monkeypatch, path)
    out = path.read_text()

    assert patch.MARKER in out
    # The hardcoded defaults are gone, replaced by the inherit-or-override exprs.
    assert '"claude-opus-4-6"' not in out
    assert "process.env.EVEN_MODEL || _ppMeta.model" in out
    assert "process.env.EVEN_PERMISSION_MODE || _ppMeta.permissionMode" in out
    assert "_ppMeta.cwd || this.lockedCwd" in out
    assert "Number(process.env.EVEN_MAX_TURNS) || 100000" in out
    assert "maxTurns: 50," not in out
    # The meta is resolved once, just before the query call.
    assert "const _ppMeta = _ppSessionMeta(this.sessionId);" in out
    # A timestamped backup of the original was written.
    assert list(tmp_path.glob("session.js.bak-*"))


@_needs_node
def test_apply_is_idempotent(monkeypatch, tmp_path: Path, capsys) -> None:
    path = _write_fixture(tmp_path)
    _run_main(monkeypatch, path)
    after_first = path.read_text()
    capsys.readouterr()

    _run_main(monkeypatch, path)
    assert path.read_text() == after_first
    assert "Already patched" in capsys.readouterr().out
    # No second backup from the no-op re-run.
    assert len(list(tmp_path.glob("session.js.bak-*"))) == 1


@pytest.mark.parametrize(
    "mangle",
    [
        pytest.param(lambda t: t.replace("                maxTurns: 50,\n", ""), id="missing"),
        pytest.param(
            lambda t: t.replace(
                "                maxTurns: 50,",
                "                maxTurns: 50,\n                maxTurns: 50,",
            ),
            id="duplicated",
        ),
    ],
)
def test_apply_aborts_when_an_anchor_does_not_match_exactly_once(
    monkeypatch, tmp_path: Path, mangle
) -> None:
    path = _write_fixture(tmp_path, mangle(FIXTURE_SESSION_JS))
    original = path.read_text()

    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, path)

    assert "re-inspect" in str(exc.value)
    # Fail loud means touch nothing: file unchanged, no backup written.
    assert path.read_text() == original
    assert not list(tmp_path.glob("session.js.bak-*"))


def test_check_reports_state_without_writing(monkeypatch, tmp_path: Path, capsys) -> None:
    path = _write_fixture(tmp_path)

    _run_main(monkeypatch, path, "--check")
    assert "UNPATCHED" in capsys.readouterr().out

    # Simulate an already-patched file and confirm --check sees it, still no writes.
    path.write_text(f"// {patch.MARKER}\n{FIXTURE_SESSION_JS}")
    _run_main(monkeypatch, path, "--check")
    assert "PATCHED" in capsys.readouterr().out
    assert not list(tmp_path.glob("session.js.bak-*"))


def test_restore_recovers_the_latest_backup(monkeypatch, tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    backup = tmp_path / "session.js.bak-20260101T000000"
    backup.write_text(FIXTURE_SESSION_JS)

    path.write_text("// clobbered by a botched patch\n")
    _run_main(monkeypatch, path, "--restore")
    assert path.read_text() == FIXTURE_SESSION_JS


def test_unknown_mode_exits(monkeypatch, tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, path, "--bogus")
    assert "unknown mode" in str(exc.value)


@_needs_node
def test_node_syntax_ok_discriminates_valid_from_broken(tmp_path: Path) -> None:
    good = tmp_path / "good.mjs"
    good.write_text("export const x = 1;\n")
    bad = tmp_path / "bad.mjs"
    bad.write_text("export const x = ;\n")
    assert patch.node_syntax_ok(good) is True
    assert patch.node_syntax_ok(bad) is False


# --------------------------------------------------------------------------------------
# The embedded transcript reader (_ppSessionMeta), run under the real node runtime
# --------------------------------------------------------------------------------------


def _run_reader(
    tmp_path: Path,
    transcript_lines: list[dict] | None,
    session_id: str,
    bucket: str = "some-project",
) -> dict:
    """Run the injected `_ppSessionMeta` JS against a fixture transcript under a fake HOME.

    `bucket` is the project-bucket directory the transcript is written into; the cwd
    resolver only captures a recorded cwd whose encoding matches it, so cwd tests pass
    a bucket derived from the cwd they expect (see `_bucket`).

    Returns the parsed `{model?, permissionMode?, cwd?}` object (undefined fields are
    absent, matching JSON.stringify semantics).
    """
    home = tmp_path / "home"
    if transcript_lines is not None:
        proj = home / ".claude" / "projects" / bucket
        proj.mkdir(parents=True)
        (proj / f"{session_id}.jsonl").write_text(
            "\n".join(json.dumps(line) for line in transcript_lines) + "\n"
        )
    else:
        (home / ".claude" / "projects").mkdir(parents=True)

    harness = tmp_path / "harness.mjs"
    harness.write_text(
        patch.IMPORT_REPLACEMENT
        + '\nconsole.log(JSON.stringify(_ppSessionMeta(process.argv[2] || "")));\n'
    )
    result = subprocess.run(
        ["node", str(harness), session_id],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "HOME": str(home)},
    )
    return json.loads(result.stdout)


@_needs_node
def test_reader_inherits_model_and_permission_mode(tmp_path: Path) -> None:
    meta = _run_reader(
        tmp_path,
        [
            {"type": "assistant", "message": {"model": "claude-opus-4-8"}},
            {"type": "permission-mode", "permissionMode": "auto"},
        ],
        "sess-1",
    )
    assert meta == {"model": "claude-opus-4-8", "permissionMode": "auto"}


@_needs_node
def test_reader_skips_synthetic_model_turns(tmp_path: Path) -> None:
    # The last assistant turn is synthetic; the reader must fall back to the last
    # real claude-* turn rather than leak "<synthetic>" into the query.
    meta = _run_reader(
        tmp_path,
        [
            {"type": "assistant", "message": {"model": "claude-opus-4-8"}},
            {"type": "assistant", "message": {"model": "<synthetic>"}},
        ],
        "sess-1",
    )
    assert meta["model"] == "claude-opus-4-8"


@_needs_node
def test_reader_strips_model_suffix(tmp_path: Path) -> None:
    meta = _run_reader(
        tmp_path,
        [{"type": "assistant", "message": {"model": "claude-opus-4-8[1m]"}}],
        "sess-1",
    )
    assert meta["model"] == "claude-opus-4-8"


@_needs_node
def test_reader_takes_the_last_permission_mode(tmp_path: Path) -> None:
    meta = _run_reader(
        tmp_path,
        [
            {"type": "permission-mode", "permissionMode": "acceptEdits"},
            {"type": "permission-mode", "permissionMode": "auto"},
        ],
        "sess-1",
    )
    assert meta["permissionMode"] == "auto"


@_needs_node
def test_reader_returns_empty_without_a_session_id(tmp_path: Path) -> None:
    assert _run_reader(tmp_path, [{"type": "permission-mode", "permissionMode": "auto"}], "") == {}


@_needs_node
def test_reader_returns_empty_when_transcript_missing(tmp_path: Path) -> None:
    assert _run_reader(tmp_path, None, "no-such-session") == {}


@_needs_node
def test_reader_ignores_non_claude_models(tmp_path: Path) -> None:
    # Only a synthetic model is present -> no model, but the permission mode still resolves.
    meta = _run_reader(
        tmp_path,
        [
            {"type": "assistant", "message": {"model": "<synthetic>"}},
            {"type": "permission-mode", "permissionMode": "plan"},
        ],
        "sess-1",
    )
    assert "model" not in meta
    assert meta["permissionMode"] == "plan"


@_needs_node
def test_reader_resolves_cwd_to_the_transcripts_bucket(tmp_path: Path) -> None:
    # The session was created at the project root, then cd'd into a subdir. Its
    # transcript stays in the root's bucket, so the resume cwd must be the root —
    # launching from the (more recent, still-existing) subdir hits a different,
    # transcript-less bucket and yields "no conversation found".
    root = tmp_path / "proj"
    root.mkdir()
    sub = root / "subdir"
    sub.mkdir()
    meta = _run_reader(
        tmp_path,
        [
            {"type": "user", "cwd": str(root)},
            {"type": "user", "cwd": str(sub)},
        ],
        "sess-1",
        bucket=_bucket(str(root)),
    )
    assert meta["cwd"] == str(root)


@_needs_node
def test_reader_skips_the_bucket_cwd_when_it_no_longer_exists(tmp_path: Path) -> None:
    # The cwd that owns the transcript's bucket was deleted; don't hand a dead path
    # to the SDK — leave cwd unset so it falls back to lockedCwd.
    root = tmp_path / "gone"  # bucket matches, but never created on disk
    meta = _run_reader(
        tmp_path,
        [{"type": "user", "cwd": str(root)}],
        "sess-1",
        bucket=_bucket(str(root)),
    )
    assert "cwd" not in meta


@_needs_node
def test_reader_ignores_a_subdir_cwd_whose_bucket_does_not_match(tmp_path: Path) -> None:
    # Only the subdir cwd is recorded, but the transcript lives in the root's bucket.
    # The subdir encodes to a different bucket, so it must not be captured — better an
    # unset cwd (fall back to lockedCwd) than a cwd `--resume` can't locate.
    root = tmp_path / "proj"
    root.mkdir()
    sub = root / "subdir"
    sub.mkdir()
    meta = _run_reader(
        tmp_path,
        [{"type": "user", "cwd": str(sub)}],
        "sess-1",
        bucket=_bucket(str(root)),
    )
    assert "cwd" not in meta


@_needs_node
def test_reader_ignores_an_empty_cwd(tmp_path: Path) -> None:
    # existsSync("") is falsy, so an empty cwd must not be captured — otherwise it would
    # clobber a live lockedCwd through the `_ppMeta.cwd || this.lockedCwd` fallback.
    meta = _run_reader(tmp_path, [{"type": "user", "cwd": ""}], "sess-1")
    assert "cwd" not in meta
