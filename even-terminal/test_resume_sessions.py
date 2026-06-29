"""Tests for `utilities/even-terminal/resume-sessions.py`.

The script's job is to read free-sessions.py's handoff, decide which freed sessions the
glasses actually continued, and build the tmux invocations to reopen them. The decision
logic (`parse_state`, `classify`, `resume_shell_command`, `partition_by_live_pane`,
`fresh_session_names`, `fresh_tmux_commands`, `iterm_open_command`, `resume_commands`)
is pure and takes transcript mtimes as an injected function, so it's tested here without
a real ~/.claude tree or a running tmux. The thin I/O wrappers (`transcript_mtime`,
`launch`, `tmux_session_exists`, `tmux_pane_ids`) and the iTerm/osascript orchestration
in `main` are not unit-tested.

The script has a hyphen in its name, so it's loaded by file path; it's registered in
sys.modules before exec so its `@dataclass`es resolve under `from __future__ import
annotations`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent / "resume-sessions.py"
_spec = importlib.util.spec_from_file_location("resume_sessions", _SCRIPT)
assert _spec and _spec.loader
rs = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rs
_spec.loader.exec_module(rs)


@pytest.fixture(autouse=True)
def _no_return_note(monkeypatch):
    """Neutralize the laptop-return note by default.

    Most tests assert the tmux/pane plumbing, not the note, so they expect the bare
    `cd … && claude --resume …` form. The note-specific tests set HIKE_RESUME_NOTE
    explicitly to override this.
    """
    monkeypatch.setenv("HIKE_RESUME_NOTE", "")


def _state(freed_at: float, sessions: list[dict]) -> rs.FreedState:
    return rs.parse_state(json.dumps({"freed_at": freed_at, "sessions": sessions}))


# --------------------------------------------------------------------------------------
# parse_state
# --------------------------------------------------------------------------------------


def test_parse_state_reads_the_handoff() -> None:
    state = _state(
        100.0,
        [{"session_id": "sess-a", "cwd": "/work/a", "name": "proj-a"}],
    )
    assert state.freed_at == 100.0
    assert state.sessions == [rs.FreedSession("sess-a", "/work/a", "proj-a")]


def test_parse_state_allows_missing_name() -> None:
    state = _state(1.0, [{"session_id": "s", "cwd": "/x"}])
    assert state.sessions[0].name is None


def test_parse_state_reads_the_pane() -> None:
    state = _state(1.0, [{"session_id": "s", "cwd": "/x", "name": "n", "pane": "%4"}])
    assert state.sessions[0].pane == "%4"


def test_parse_state_allows_missing_pane() -> None:
    # Older handoffs (and non-tmux sessions) have no pane -> resume falls back to a window.
    state = _state(1.0, [{"session_id": "s", "cwd": "/x"}])
    assert state.sessions[0].pane is None


def test_parse_state_rejects_a_malformed_entry() -> None:
    # Missing cwd -> we can't resume it, so the whole handoff is rejected loudly.
    with pytest.raises(ValueError, match="malformed"):
        _state(1.0, [{"session_id": "s"}])


# --------------------------------------------------------------------------------------
# classify — continued vs untouched vs missing
# --------------------------------------------------------------------------------------


def test_classify_splits_on_whether_the_transcript_changed_after_free() -> None:
    state = _state(
        100.0,
        [
            {"session_id": "continued", "cwd": "/work/c", "name": "did-work"},
            {"session_id": "untouched", "cwd": "/work/u", "name": "left-alone"},
            {"session_id": "gone", "cwd": "/work/g", "name": "no-transcript"},
        ],
    )
    mtimes = {"continued": 150.0, "untouched": 50.0, "gone": None}
    continued, untouched, missing = rs.classify(state, lambda s: mtimes[s.session_id])

    assert [s.session_id for s in continued] == ["continued"]
    assert [s.session_id for s in untouched] == ["untouched"]
    assert [s.session_id for s in missing] == ["gone"]


def test_classify_treats_exactly_equal_mtime_as_untouched() -> None:
    # mtime == freed_at means no new turn landed after the free, so it's not "continued".
    state = _state(100.0, [{"session_id": "s", "cwd": "/x"}])
    continued, untouched, _ = rs.classify(state, lambda s: 100.0)
    assert not continued
    assert [s.session_id for s in untouched] == ["s"]


# --------------------------------------------------------------------------------------
# command construction
# --------------------------------------------------------------------------------------


def test_resume_shell_command_cds_then_resumes() -> None:
    s = rs.FreedSession("sess-a", "/work/a", "proj-a")
    assert rs.resume_shell_command(s) == "cd /work/a && claude --resume sess-a"


def test_resume_shell_command_quotes_a_cwd_with_a_space() -> None:
    # send-keys types this verbatim into the pane's shell, so a spaced path must be
    # quoted or the resume `cd`s into the wrong directory.
    s = rs.FreedSession("sess-a", "/work/my proj", None)
    assert rs.resume_shell_command(s) == "cd '/work/my proj' && claude --resume sess-a"


def test_resume_shell_command_appends_the_return_note(monkeypatch) -> None:
    # The note rides in as a positional prompt (shell-quoted), so claude delivers it
    # as the first turn of the resumed session.
    monkeypatch.setenv("HIKE_RESUME_NOTE", "back home")
    s = rs.FreedSession("sess-a", "/work/a", "proj-a")
    assert rs.resume_shell_command(s) == "cd /work/a && claude --resume sess-a 'back home'"


def test_resume_shell_command_default_note_rides_along(monkeypatch) -> None:
    # With no override, the shipped default note is appended (a real first-turn prompt).
    monkeypatch.delenv("HIKE_RESUME_NOTE", raising=False)
    s = rs.FreedSession("sess-a", "/work/a", "proj-a")
    cmd = rs.resume_shell_command(s)
    assert cmd.startswith("cd /work/a && claude --resume sess-a ")
    assert "hike-mode" in cmd


def test_resume_shell_command_empty_note_sends_no_prompt(monkeypatch) -> None:
    # HIKE_RESUME_NOTE="" (or whitespace) opts out: a bare resume, no positional prompt.
    monkeypatch.setenv("HIKE_RESUME_NOTE", "   ")
    s = rs.FreedSession("sess-a", "/work/a", "proj-a")
    assert rs.resume_shell_command(s) == "cd /work/a && claude --resume sess-a"


def test_sanitize_session_name_keeps_safe_chars_and_collapses_the_rest() -> None:
    # '.' and ':' are illegal in tmux session names; spaces/quotes break the shell +
    # AppleScript interpolation. All collapse to '_'.
    assert rs.sanitize_session_name("proj.a:1 b") == "proj_a_1_b"
    assert rs.sanitize_session_name("ok-name_2") == "ok-name_2"
    assert rs.sanitize_session_name("...") == "___"


def test_fresh_session_names_are_unique_and_sanitized() -> None:
    sessions = [
        rs.FreedSession("sess-a", "/work/a", "proj.a"),  # sanitized to proj_a
        rs.FreedSession("sess-b", "/work/b", "proj.a"),  # collides -> proj_a-2
        rs.FreedSession("abcdef1234567890", "/work/c", None),  # unnamed -> short id
    ]
    assert rs.fresh_session_names(sessions) == ["proj_a", "proj_a-2", "abcdef12"]


def test_fresh_tmux_commands_makes_one_session_per_entry() -> None:
    named = [
        ("proj-a", rs.FreedSession("sess-a", "/work/a", "proj-a")),
        ("proj-b", rs.FreedSession("sess-b", "/work/b", "proj-b")),
    ]
    cmds = rs.fresh_tmux_commands(named)
    # each session: its own detached new-session, then send-keys into it
    assert cmds[0] == ["tmux", "new-session", "-d", "-s", "proj-a", "-c", "/work/a"]
    assert cmds[1] == [
        "tmux",
        "send-keys",
        "-t",
        "proj-a",
        "cd /work/a && claude --resume sess-a",
        "Enter",
    ]
    assert cmds[2] == ["tmux", "new-session", "-d", "-s", "proj-b", "-c", "/work/b"]
    assert cmds[3] == [
        "tmux",
        "send-keys",
        "-t",
        "proj-b",
        "cd /work/b && claude --resume sess-b",
        "Enter",
    ]


def test_iterm_open_command_attaches_to_the_named_session() -> None:
    cmd = rs.iterm_open_command("proj-a")
    assert cmd[0] == "osascript"
    assert cmd[1] == "-e"
    assert "tmux attach -t proj-a" in cmd[2]
    assert 'tell application "iTerm"' in cmd[2]


# --------------------------------------------------------------------------------------
# in-place resume — send-keys back into a surviving pane, fresh windows for the rest
# --------------------------------------------------------------------------------------


def test_partition_by_live_pane_splits_on_pane_liveness() -> None:
    alive = rs.FreedSession("sess-a", "/work/a", "proj-a", pane="%4")  # pane still up
    gone = rs.FreedSession("sess-b", "/work/b", "proj-b", pane="%9")  # pane closed
    never = rs.FreedSession("sess-c", "/work/c", "proj-c")  # never in tmux
    in_place, fresh = rs.partition_by_live_pane([alive, gone, never], frozenset({"%4"}))
    assert in_place == [("%4", alive)]
    assert fresh == [gone, never]


def test_send_keys_command_types_resume_into_the_pane() -> None:
    s = rs.FreedSession("sess-a", "/work/a", "proj-a", pane="%4")
    assert rs.send_keys_command("%4", s) == [
        "tmux",
        "send-keys",
        "-t",
        "%4",
        "cd /work/a && claude --resume sess-a",
        "Enter",
    ]


def test_resume_commands_sends_in_place_then_opens_a_session_per_fresh() -> None:
    alive = rs.FreedSession("sess-a", "/work/a", "proj-a", pane="%4")
    gone = rs.FreedSession("sess-b", "/work/b", "proj-b", pane="%9")
    in_place, fresh = rs.partition_by_live_pane([alive, gone], frozenset({"%4"}))
    fresh_named = list(zip(rs.fresh_session_names(fresh), fresh))
    cmds = rs.resume_commands(in_place, fresh_named)

    # in-place first: send-keys straight into the surviving pane %4
    assert cmds[0] == [
        "tmux",
        "send-keys",
        "-t",
        "%4",
        "cd /work/a && claude --resume sess-a",
        "Enter",
    ]
    # then a dedicated tmux session for the one whose pane is gone
    assert cmds[1] == ["tmux", "new-session", "-d", "-s", "proj-b", "-c", "/work/b"]
    assert cmds[2] == [
        "tmux",
        "send-keys",
        "-t",
        "proj-b",
        "cd /work/b && claude --resume sess-b",
        "Enter",
    ]


def test_resume_commands_with_only_in_place_creates_no_session() -> None:
    # All panes survived -> no new tmux session, just send-keys into each.
    a = rs.FreedSession("sess-a", "/work/a", "proj-a", pane="%4")
    in_place, fresh = rs.partition_by_live_pane([a], frozenset({"%4"}))
    fresh_named = list(zip(rs.fresh_session_names(fresh), fresh))
    cmds = rs.resume_commands(in_place, fresh_named)
    assert cmds == [
        ["tmux", "send-keys", "-t", "%4", "cd /work/a && claude --resume sess-a", "Enter"],
    ]
