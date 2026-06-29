#!/usr/bin/env python3
"""Reopen the sessions you continued on the glasses — back on the laptop, at the point
the glasses left off, not the stale state your old terminal tab showed.

Pairs with free-sessions.py. Before a hike, `free-sessions.py --free` terminates the
laptop processes holding your sessions (so the glasses can resume them) and records
what it freed, and when, in ~/.hike/freed-sessions.json. even-terminal resumes a
session *in place* (`resume: <id>`, no fork), so every glasses turn appends to the
SAME ~/.claude/projects/<bucket>/<id>.jsonl. Back home, this script reads that record,
finds which of the freed sessions actually got new turns during the hike (transcript
modified after it was freed), and reopens each running `claude --resume <id>` in the
right directory — landing you exactly where the glasses left off.

Where it reopens depends on whether the original tmux pane survived. free-sessions.py
records each freed session's pane id; if that pane still exists (tmux keeps panes alive
across sleep and terminal close), this sends `claude --resume` straight back into it —
the same pane you were working in. If the pane is gone (or the session was never in
tmux), it gets its OWN new tmux session (named after the session) and, by default, an
iTerm window opened and attached to it — so each lands in a separate, spread-able Mac
window rather than stacked as windows inside one session.

Sessions that were freed but never touched on the glasses are reported but not
reopened by default (they're unchanged from when you left); pass --all for those too.

Safe by default: with no flags it only LISTS continued vs untouched. Pass --launch to
reopen them.

Usage:
  python3 resume-sessions.py                  # list continued vs untouched, no changes
  python3 resume-sessions.py --launch         # resume each: in place if its pane survived,
                                              #   else its own tmux session + iTerm window
  python3 resume-sessions.py --launch --all   # also reopen the untouched freed sessions
  python3 resume-sessions.py --launch --no-open  # make the tmux sessions but don't open iTerm
  # then attach any that didn't auto-open: tmux attach -t <name>
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Where hike-mode keeps its state. Override with HIKE_DIR to relocate it
# (must match whatever free-sessions.py used).
HIKE_DIR = Path(os.environ.get("HIKE_DIR") or Path.home() / ".hike")
FREED_STATE = HIKE_DIR / "freed-sessions.json"


@dataclass(frozen=True)
class FreedSession:
    """One entry from freed-sessions.json — a session free-sessions.py released."""

    session_id: str
    cwd: str
    name: str | None = None
    pane: str | None = None


@dataclass(frozen=True)
class FreedState:
    freed_at: float
    sessions: list[FreedSession]


def parse_state(text: str) -> FreedState:
    """Parse freed-sessions.json. Raises ValueError on a malformed/empty handoff."""
    o = json.loads(text)
    try:
        freed_at = float(o["freed_at"])
        sessions = [
            FreedSession(
                session_id=str(s["session_id"]),
                cwd=str(s["cwd"]),
                name=str(s["name"]) if s.get("name") is not None else None,
                pane=str(s["pane"]) if s.get("pane") is not None else None,
            )
            for s in o["sessions"]
        ]
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"malformed freed-sessions.json: {e}") from e
    return FreedState(freed_at=freed_at, sessions=sessions)


# Delivered as the first turn when a session is reopened on the laptop, so Claude
# knows the glasses drove it during the hike and should drop any HUD/terse mode.
# `claude --resume <id> "<note>"` sends it as the opening message while staying
# interactive. Override with HIKE_RESUME_NOTE; set it empty to resume with no note.
DEFAULT_RESUME_NOTE = (
    "[hike-mode] Back at the laptop now - the glasses had this session during the "
    "hike. Resume normal full-width replies and drop any HUD/terse rendering."
)


def resume_note() -> str:
    """The first-turn note for a laptop resume - env-overridable, possibly empty."""
    return os.environ.get("HIKE_RESUME_NOTE", DEFAULT_RESUME_NOTE).strip()


def resume_shell_command(s: FreedSession) -> str:
    """The shell command that lands back in this session at its latest state.

    Both fields are shell-quoted because this string is typed verbatim into a pane's
    shell via `tmux send-keys` — an unquoted cwd with a space would `cd` to the wrong
    directory (or fail) instead of resuming where the glasses left off.

    Unless HIKE_RESUME_NOTE is empty, a return note is appended as a positional prompt
    (`claude --resume <id> "<note>"`), which the CLI delivers as the first turn while
    keeping the session interactive — so Claude knows the hike is over.
    """
    cmd = f"cd {shlex.quote(s.cwd)} && claude --resume {shlex.quote(s.session_id)}"
    note = resume_note()
    if note:
        cmd += f" {shlex.quote(note)}"
    return cmd


def classify(
    state: FreedState,
    mtime_of: Callable[[FreedSession], float | None],
) -> tuple[list[FreedSession], list[FreedSession], list[FreedSession]]:
    """Split freed sessions into (continued, untouched, missing) given transcript mtimes.

    A session is *continued* if its transcript was modified after it was freed — i.e.
    the glasses appended turns during the hike. *untouched* means the transcript exists
    but predates the free (you never opened it on the glasses). *missing* means no
    transcript was found.

    mtime_of is injected (returns the transcript mtime, or None if absent) so the
    split is pure and testable without touching the filesystem.
    """
    continued, untouched, missing = [], [], []
    for s in state.sessions:
        mtime = mtime_of(s)
        if mtime is None:
            missing.append(s)
        elif mtime > state.freed_at:
            continued.append(s)
        else:
            untouched.append(s)
    return continued, untouched, missing


def sanitize_session_name(raw: str) -> str:
    """A tmux/shell/AppleScript-safe session name.

    tmux forbids ``.`` and ``:`` in session names, and the name is interpolated into both
    a shell command and an AppleScript string, so we keep only ``[A-Za-z0-9_-]`` and
    collapse everything else to ``_`` (falling back to ``session`` if nothing survives).
    """
    return re.sub(r"[^A-Za-z0-9_-]", "_", raw) or "session"


def fresh_session_names(sessions: list[FreedSession]) -> list[str]:
    """One unique, sanitized tmux session name per fresh session (its name or short id).

    De-duplicates by suffixing ``-2``, ``-3``, … so two sessions with the same name (or
    none) still get distinct tmux sessions.
    """
    taken: set[str] = set()
    names: list[str] = []
    for s in sessions:
        base = sanitize_session_name(s.name or s.session_id[:8])
        name = base
        i = 2
        while name in taken:
            name = f"{base}-{i}"
            i += 1
        taken.add(name)
        names.append(name)
    return names


def fresh_tmux_commands(named: list[tuple[str, FreedSession]]) -> list[list[str]]:
    """Per fresh session: create its OWN detached tmux session and send the resume command.

    One session per claude (not one session with many windows) so each can be attached in
    its own terminal window and spread across the screen.
    """
    cmds: list[list[str]] = []
    for name, s in named:
        cmds.append(["tmux", "new-session", "-d", "-s", name, "-c", s.cwd])
        cmds.append(["tmux", "send-keys", "-t", name, resume_shell_command(s), "Enter"])
    return cmds


def iterm_open_command(session_name: str) -> list[str]:
    """osascript argv to open an iTerm window attached to a tmux session.

    iTerm-specific by design (the user opted into auto-open); on a non-iTerm setup the
    call simply fails and the caller falls back to printing the attach command. The name
    is pre-sanitized to ``[A-Za-z0-9_-]``, so it's safe to interpolate into the script.
    """
    script = (
        'tell application "iTerm"\n'
        "  create window with default profile\n"
        "  tell current session of current window to write text "
        f'"tmux attach -t {session_name}"\n'
        "end tell"
    )
    return ["osascript", "-e", script]


def send_keys_command(pane: str, s: FreedSession) -> list[str]:
    """tmux argv to type the resume command into an existing pane (in-place resume)."""
    return ["tmux", "send-keys", "-t", pane, resume_shell_command(s), "Enter"]


def partition_by_live_pane(
    sessions: list[FreedSession],
    live_panes: frozenset[str],
) -> tuple[list[tuple[str, FreedSession]], list[FreedSession]]:
    """Split sessions into (in_place, fresh) by whether their freed pane still exists.

    *in_place* — the pane it was freed from is still live, so we send `claude --resume`
    straight back into it (paired with that pane id). *fresh* — no pane was recorded, or
    that pane is gone (terminal closed / tmux restarted), so it gets a new window.

    Pure (the live-pane set is injected) so it's testable without a running tmux.
    """
    in_place: list[tuple[str, FreedSession]] = []
    fresh: list[FreedSession] = []
    for s in sessions:
        if s.pane is not None and s.pane in live_panes:
            in_place.append((s.pane, s))
        else:
            fresh.append(s)
    return in_place, fresh


def resume_commands(
    in_place: list[tuple[str, FreedSession]],
    fresh_named: list[tuple[str, FreedSession]],
) -> list[list[str]]:
    """All tmux invocations: send-keys into surviving panes, a new session per fresh one."""
    cmds: list[list[str]] = [send_keys_command(pane, s) for pane, s in in_place]
    cmds.extend(fresh_tmux_commands(fresh_named))
    return cmds


def transcript_mtime(session_id: str) -> float | None:
    """Newest mtime of this session's transcript across all project buckets, or None.

    The transcript is bucketed by the session's *creation* cwd, which may differ from
    where it was last run, so we scan every bucket rather than guessing the path.
    """
    candidates = list(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    if not candidates:
        return None
    return max(p.stat().st_mtime for p in candidates)


def tmux_session_exists(session_name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name], capture_output=True, text=True
    )
    return result.returncode == 0


def tmux_pane_ids() -> frozenset[str]:
    """Every currently-live tmux pane id, or empty if no tmux server is running."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return frozenset()
    return frozenset(result.stdout.split())


def launch(commands: list[list[str]]) -> None:
    for cmd in commands:
        subprocess.run(cmd, check=True)


def main() -> None:
    args = sys.argv[1:]
    do_launch = "--launch" in args
    include_untouched = "--all" in args
    open_windows = "--no-open" not in args
    for arg in args:
        if arg not in ("--launch", "--all", "--no-open"):
            sys.exit(f"ERROR: unknown argument {arg!r}. Use --launch, --all, --no-open.")

    if not FREED_STATE.exists():
        sys.exit(f"ERROR: {FREED_STATE} not found. Run free-sessions.py --free before the hike.")
    state = parse_state(FREED_STATE.read_text())

    continued, untouched, missing = classify(state, lambda s: transcript_mtime(s.session_id))

    if continued:
        print(f"{len(continued)} session(s) continued on the glasses (will reopen):\n")
        for s in continued:
            print(f"  {s.name or '(unnamed)'}\n    {resume_shell_command(s)}")
    else:
        print("No freed session was continued on the glasses.")
    if untouched:
        print(f"\n{len(untouched)} freed but untouched (use --all to reopen):")
        for s in untouched:
            print(f"  {s.name or '(unnamed)'}  ({s.session_id})")
    if missing:
        print(f"\n{len(missing)} freed session(s) have no transcript on disk (skipped):")
        for s in missing:
            print(f"  {s.name or '(unnamed)'}  ({s.session_id})")

    to_open = continued + untouched if include_untouched else continued
    if not to_open:
        return

    if not do_launch:
        print("\n(dry run — re-run with --launch to reopen these.)")
        return

    in_place, fresh = partition_by_live_pane(to_open, tmux_pane_ids())
    fresh_named = list(zip(fresh_session_names(fresh), fresh))

    for name, _ in fresh_named:
        if tmux_session_exists(name):
            sys.exit(
                f"ERROR: tmux session {name!r} already exists. Attach with "
                f"`tmux attach -t {name}`, or close it first."
            )
    launch(resume_commands(in_place, fresh_named))

    if in_place:
        print(f"\nResumed {len(in_place)} session(s) in place — sent into their original panes.")
    if fresh:
        print(f"\nOpened {len(fresh)} tmux session(s), one per resumed session:")
        for name, _ in fresh_named:
            print(f"  {name}")
        if open_windows:
            opened = sum(
                subprocess.run(iterm_open_command(name), capture_output=True, text=True).returncode
                == 0
                for name, _ in fresh_named
            )
            if opened == len(fresh_named):
                print(f"Opened {opened} iTerm window(s), one per session.")
            else:
                print(f"Opened {opened}/{len(fresh_named)} iTerm window(s); attach the rest:")
                for name, _ in fresh_named:
                    print(f"  tmux attach -t {name}")
        else:
            print("Attach each in its own window:")
            for name, _ in fresh_named:
                print(f"  tmux attach -t {name}")


if __name__ == "__main__":
    main()
