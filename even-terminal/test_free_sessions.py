"""Tests for `utilities/even-terminal/free-sessions.py`.

The risky part of this script kills processes, so the policy that decides *which*
processes — interactive-only, never the running session, honour --keep, live only —
is factored into pure functions (`parse_registration`, `is_laptop_holder`,
`matches_keep`, `select_targets`, `resume_command`) that take already-resolved
liveness as input. Those are exercised here without spawning or killing anything.

The script lives next to this file (not on the package path), so it's loaded by file
path. The process-touching functions (`terminate`, `pid_alive`, `ancestor_pids`) are
thin stdlib/ps wrappers and are not unit-tested here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "free-sessions.py"
_spec = importlib.util.spec_from_file_location("free_sessions", _SCRIPT)
assert _spec and _spec.loader
fs = importlib.util.module_from_spec(_spec)
# Register before exec so the `@dataclass` can resolve its own module under
# `from __future__ import annotations` (dataclasses looks the module up in sys.modules).
sys.modules[_spec.name] = fs
_spec.loader.exec_module(fs)


def _reg(
    pid: int,
    session_id: str,
    *,
    cwd: str = "/work/proj",
    kind: str = "interactive",
    entrypoint: str = "cli",
    name: str | None = None,
) -> dict:
    reg = {
        "pid": pid,
        "sessionId": session_id,
        "cwd": cwd,
        "kind": kind,
        "entrypoint": entrypoint,
    }
    if name is not None:
        reg["name"] = name
    return reg


# --------------------------------------------------------------------------------------
# parse_registration
# --------------------------------------------------------------------------------------


def test_parse_registration_reads_a_well_formed_file() -> None:
    h = fs.parse_registration(json.dumps(_reg(123, "sess-a", name="proj-a")))
    assert h is not None
    assert (h.pid, h.session_id, h.kind, h.entrypoint, h.name) == (
        123,
        "sess-a",
        "interactive",
        "cli",
        "proj-a",
    )


def test_parse_registration_returns_none_on_bad_json() -> None:
    assert fs.parse_registration("{not json") is None


def test_parse_registration_returns_none_when_a_keyed_field_is_missing() -> None:
    # No entrypoint -> we can't classify it, so it's skipped rather than guessed.
    incomplete = {"pid": 1, "sessionId": "s", "cwd": "/x", "kind": "interactive"}
    assert fs.parse_registration(json.dumps(incomplete)) is None


def test_parse_registration_coerces_a_non_string_name() -> None:
    # name is coerced at the boundary like every sibling field, so it never violates
    # the str | None annotation and can't silently defeat --keep matching downstream.
    h = fs.parse_registration(json.dumps(_reg(1, "s", name=123)))  # type: ignore[arg-type]
    assert h is not None
    assert h.name == "123"


# --------------------------------------------------------------------------------------
# classification helpers
# --------------------------------------------------------------------------------------


def test_is_laptop_holder_accepts_only_interactive_cli() -> None:
    assert fs.is_laptop_holder(fs.Holder(1, "s", "/x", "interactive", "cli")) is True
    # even-terminal's own glasses spawn — same session, not a laptop tab
    assert fs.is_laptop_holder(fs.Holder(1, "s", "/x", "interactive", "sdk-ts")) is False
    # a background job
    assert fs.is_laptop_holder(fs.Holder(1, "s", "/x", "bg", "cli")) is False


def test_matches_keep_by_id_or_name() -> None:
    h = fs.Holder(1, "sess-a", "/x", "interactive", "cli", name="proj-a")
    assert fs.matches_keep(h, frozenset({"sess-a"})) is True
    assert fs.matches_keep(h, frozenset({"proj-a"})) is True
    assert fs.matches_keep(h, frozenset({"something-else"})) is False


def test_resume_command_is_a_cd_plus_resume_with_a_label() -> None:
    h = fs.Holder(1, "sess-a", "/work/proj", "interactive", "cli", name="proj-a")
    cmd = fs.resume_command(h)
    assert "cd /work/proj && claude --resume sess-a" in cmd
    assert "proj-a" in cmd


def test_resume_command_quotes_a_cwd_with_a_space() -> None:
    # A path with a space must be shell-quoted or the `cd` lands in the wrong directory.
    h = fs.Holder(1, "sess-a", "/work/my proj", "interactive", "cli")
    cmd = fs.resume_command(h)
    assert "cd '/work/my proj' && claude --resume sess-a" in cmd


# --------------------------------------------------------------------------------------
# select_targets — the load-bearing policy
# --------------------------------------------------------------------------------------


def _holders() -> list[fs.Holder]:
    return [
        fs.Holder(10, "sess-self", "/work/self", "interactive", "cli", name="current"),
        fs.Holder(
            11, "sess-self", "/work/self", "interactive", "sdk-ts"
        ),  # glasses spawn, same session
        fs.Holder(20, "sess-a", "/work/a", "interactive", "cli", name="proj-a"),
        fs.Holder(21, "sess-b", "/work/b", "interactive", "cli", name="proj-b"),
        fs.Holder(30, "sess-bg", "/work/bg", "bg", "cli", name="job"),  # background
        fs.Holder(40, "sess-dead", "/work/d", "interactive", "cli", name="dead-tab"),  # not alive
    ]


def test_select_targets_frees_live_interactive_laptop_tabs() -> None:
    alive = frozenset({10, 11, 20, 21, 30})  # 40 is dead
    targets = fs.select_targets(_holders(), "sess-self", frozenset(), alive)
    assert {h.session_id for h in targets} == {"sess-a", "sess-b"}


def test_select_targets_never_touches_the_running_session() -> None:
    alive = frozenset({10, 11, 20, 21, 30, 40})
    targets = fs.select_targets(_holders(), "sess-self", frozenset(), alive)
    assert all(h.session_id != "sess-self" for h in targets)


def test_select_targets_excludes_dead_holders() -> None:
    alive = frozenset({10, 20})  # sess-b's pid 21 not alive
    targets = fs.select_targets(_holders(), "sess-self", frozenset(), alive)
    assert {h.session_id for h in targets} == {"sess-a"}


def test_select_targets_honours_keep() -> None:
    alive = frozenset({10, 11, 20, 21, 30})  # 40 (dead-tab) not alive
    targets = fs.select_targets(_holders(), "sess-self", frozenset({"proj-a"}), alive)
    assert {h.session_id for h in targets} == {"sess-b"}


def test_select_targets_with_no_self_session_still_excludes_non_cli() -> None:
    # Run from a plain shell (no claude ancestor): nothing to protect by session,
    # but bg / sdk-ts are still filtered out.
    alive = frozenset({10, 11, 20, 21, 30})  # 40 (dead-tab) not alive
    targets = fs.select_targets(_holders(), None, frozenset(), alive)
    assert {h.session_id for h in targets} == {"sess-self", "sess-a", "sess-b"}


def test_select_targets_protects_ancestor_pids_even_when_self_session_unresolved() -> None:
    # If the session-id lookup fails (self_session_id is None), the pid guard must still
    # keep the script from freeing its own running holder — protection fails closed.
    alive = frozenset({10, 11, 20, 21, 30})  # 40 (dead-tab) not alive
    targets = fs.select_targets(
        _holders(), None, frozenset(), alive, protected_pids=frozenset({10})
    )
    assert {h.session_id for h in targets} == {"sess-a", "sess-b"}


# --------------------------------------------------------------------------------------
# pane_for_pid — mapping a holder to its tmux pane
# --------------------------------------------------------------------------------------


def test_pane_for_pid_matches_an_ancestor_pane_root() -> None:
    # claude (pid 2000) runs under the pane's shell (pid 1896), which tmux reports as
    # pane %4's root pid — so walking up from claude finds the pane.
    ancestry = {2000: [1896, 1]}
    pane = fs.pane_for_pid(2000, {1896: "%4"}, lambda p: ancestry[p])
    assert pane == "%4"


def test_pane_for_pid_matches_the_pid_itself() -> None:
    # If claude were launched directly as the pane command, pane_pid is claude's own pid.
    pane = fs.pane_for_pid(1896, {1896: "%4"}, lambda p: [1])
    assert pane == "%4"


def test_pane_for_pid_returns_none_when_not_in_tmux() -> None:
    # Neither the pid nor any ancestor is a pane root — a plain terminal tab.
    pane = fs.pane_for_pid(2000, {1896: "%4"}, lambda p: [500, 1])
    assert pane is None


# --------------------------------------------------------------------------------------
# freed_state_payload — the handoff carries each session's pane
# --------------------------------------------------------------------------------------


def test_freed_state_payload_records_pane_per_session() -> None:
    targets = [
        fs.Holder(20, "sess-a", "/work/a", "interactive", "cli", name="proj-a"),
        fs.Holder(21, "sess-b", "/work/b", "interactive", "cli", name="proj-b"),
    ]
    panes = {"sess-a": "%4", "sess-b": None}  # b wasn't in a tmux pane
    payload = fs.freed_state_payload(targets, 123.0, panes)
    assert payload["freed_at"] == 123.0
    assert payload["sessions"] == [
        {"session_id": "sess-a", "cwd": "/work/a", "name": "proj-a", "pane": "%4"},
        {"session_id": "sess-b", "cwd": "/work/b", "name": "proj-b", "pane": None},
    ]
