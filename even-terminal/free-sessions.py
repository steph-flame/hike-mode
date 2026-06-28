#!/usr/bin/env python3
"""Release Claude Code session locks so even-terminal can resume them from the glasses.

Claude Code locks an active session: while a live `claude` process holds a session,
a second `claude --resume <id>` for the same session fails. even-terminal resumes via
exactly that path, so a session you left open in a laptop terminal can't be resumed
from the glasses — it comes back as the misleading "no conversation found with session
ID: ...".

Closing every laptop tab before a hike works but is annoying, and you lose your place.
You don't have to: a session's whole conversation lives in
`~/.claude/projects/<bucket>/<id>.jsonl`, not in the live process. Terminating the
holder process frees the lock without losing anything — you `claude --resume <id>`
later and land exactly where you were. This script does that, and prints the resume
commands so getting back is one paste.

Killing only the `claude` process leaves its terminal alive: the parent shell drops
back to its prompt, the tab/pane stays open. When that pane is a tmux pane, this script
records its pane id in the handoff so resume-sessions.py can send `claude --resume` back
into the very same pane; sessions not in tmux record no pane and resume in a fresh window.

What counts as a "laptop holder" (and gets freed):
  * a LIVE process,
  * with a registration in ~/.claude/sessions/<pid>.json,
  * whose kind is "interactive" and entrypoint is "cli".

Deliberately left alone:
  * the session this script runs under (found by walking the script's own ancestor
    pids), and every process sharing that session id — so freeing from a glasses
    session never kills the process you're talking through;
  * background jobs (kind "bg") and even-terminal's own SDK spawns (entrypoint
    "sdk-ts") — those aren't laptop tabs;
  * anything named via --keep.

Safe by default: with no flags it only LISTS what it would free. Pass --free to act.

Usage:
  python3 free-sessions.py                 # list locked sessions + resume cheatsheet, no changes
  python3 free-sessions.py --free          # terminate the holders, freeing them for resume
  python3 free-sessions.py --keep NAME_OR_ID [--keep ...]   # protect specific sessions
  python3 free-sessions.py --free --keep my-project

State lives under ~/.hike by default; set HIKE_DIR to relocate it.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

SESSIONS_DIR = Path.home() / ".claude" / "sessions"

# Where hike-mode keeps its state. Override with HIKE_DIR to relocate it.
HIKE_DIR = Path(os.environ.get("HIKE_DIR") or Path.home() / ".hike")
CHEATSHEET = HIKE_DIR / "resume-cheatsheet.txt"

# Machine-readable handoff to resume-sessions.py: which sessions were freed for the
# glasses, and when. The timestamp lets the resume side tell which ones the glasses
# actually continued (transcript modified after this) from those left untouched.
FREED_STATE = HIKE_DIR / "freed-sessions.json"

# How long to wait for a SIGTERM'd holder to exit before escalating to SIGKILL.
TERM_TIMEOUT_SEC = 5.0
TERM_POLL_SEC = 0.1


@dataclass(frozen=True)
class Holder:
    """One live `claude` process registration from ~/.claude/sessions/<pid>.json.

    Plain dataclass rather than Pydantic so the script runs under bare system
    `python3` with no venv — same constraint as the sibling even-terminal-patch.py.
    """

    pid: int
    session_id: str
    cwd: str
    kind: str
    entrypoint: str
    name: str | None = None


def parse_registration(text: str) -> Holder | None:
    """Parse one registration file's JSON into a Holder, or None if it's not usable.

    Registrations missing the fields we key on (pid/sessionId/cwd/kind/entrypoint)
    are skipped rather than guessed at.
    """
    try:
        o = json.loads(text)
    except json.JSONDecodeError:
        return None
    try:
        raw_name = o.get("name")
        return Holder(
            pid=int(o["pid"]),
            session_id=str(o["sessionId"]),
            cwd=str(o["cwd"]),
            kind=str(o["kind"]),
            entrypoint=str(o["entrypoint"]),
            name=str(raw_name) if raw_name is not None else None,
        )
    except (KeyError, TypeError, ValueError):
        return None


def is_laptop_holder(h: Holder) -> bool:
    """True if h is an interactive laptop tab (the only kind that locks for our purpose).

    Background jobs and even-terminal's own SDK spawns share session ids but are not
    laptop tabs you'd resume from the glasses, so they must not be freed.
    """
    return h.kind == "interactive" and h.entrypoint == "cli"


def matches_keep(h: Holder, keep: frozenset[str]) -> bool:
    """True if this holder was named on --keep, by either session id or session name."""
    return h.session_id in keep or (h.name is not None and h.name in keep)


def select_targets(
    holders: list[Holder],
    self_session_id: str | None,
    keep: frozenset[str],
    alive_pids: frozenset[int],
    protected_pids: frozenset[int] = frozenset(),
) -> list[Holder]:
    """Pure selection of which holders to free, given liveness already resolved.

    Kept separate from process I/O so the policy (interactive-only, never self,
    honour --keep, live only) is testable without spawning anything.

    Self-protection is belt-and-suspenders: we exclude both the running session's id
    (`self_session_id`, which also covers sibling tabs sharing it) AND the script's own
    ancestor pids (`protected_pids`). The pid guard matters because `self_session_id`
    can resolve to None if the registration lookup fails — without it, protection would
    fail *open* and `--free` could kill the process tree the script runs under.
    """
    targets = [
        h
        for h in holders
        if is_laptop_holder(h)
        and h.pid in alive_pids
        and h.pid not in protected_pids
        and h.session_id != self_session_id
        and not matches_keep(h, keep)
    ]
    return sorted(targets, key=lambda h: (h.cwd, h.session_id))


def resume_command(h: Holder) -> str:
    """The exact command to land back in this session later, with a label line."""
    label = h.name or "(unnamed)"
    return f"# {label}\ncd {shlex.quote(h.cwd)} && claude --resume {shlex.quote(h.session_id)}"


def read_registrations(sessions_dir: Path) -> list[Holder]:
    holders = []
    for path in sorted(sessions_dir.glob("*.json")):
        holder = parse_registration(path.read_text())
        if holder is not None:
            holders.append(holder)
    return holders


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — alive for our purposes (we won't touch it).
        return True
    return True


def pid_command(pid: int) -> str:
    """The process's command line via ps, or '' if it can't be read."""
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return result.stdout.strip()


def ancestor_pids(start_pid: int) -> list[int]:
    """Walk parent pids from start_pid up to the init process."""
    chain = []
    pid = start_pid
    seen = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            result = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
        except (subprocess.SubprocessError, OSError):
            break
        parent = result.stdout.strip()
        if not parent.isdigit():
            break
        pid = int(parent)
        chain.append(pid)
    return chain


def pane_for_pid(
    pid: int,
    pane_pids: Mapping[int, str],
    ancestors_of: Callable[[int], list[int]],
) -> str | None:
    """The tmux pane id whose process tree contains `pid`, or None if it's not in tmux.

    A `claude` holder runs as a child of its pane's shell, and tmux's `#{pane_pid}` is
    that pane's root process, so we test the pid itself and then each ancestor against
    the pane → root-pid map. Pure (ancestry is injected) so it's testable without a
    running tmux.
    """
    for candidate in (pid, *ancestors_of(pid)):
        pane = pane_pids.get(candidate)
        if pane is not None:
            return pane
    return None


def tmux_pane_pids() -> dict[int, str]:
    """Map each live tmux pane's root pid → its stable pane id (e.g. ``{1896: '%4'}``).

    Empty when no tmux server is running, so a non-tmux setup degrades cleanly to "no
    pane recorded" (resume then opens a fresh window) instead of erroring.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_pid} #{pane_id}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return {}
    panes: dict[int, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            panes[int(parts[0])] = parts[1]
    return panes


def find_self_session(sessions_dir: Path, ancestors: list[int]) -> str | None:
    """The session id of the nearest ancestor that has a registration, if any.

    This is what protects the running session: the script is a descendant of the
    `claude` process serving it, so that process's registration tells us which
    session id to never touch.
    """
    by_pid = {h.pid: h for h in read_registrations(sessions_dir)}
    for pid in ancestors:
        holder = by_pid.get(pid)
        if holder is not None:
            return holder.session_id
    return None


def terminate(pid: int) -> bool:
    """SIGTERM a holder, escalating to SIGKILL if it doesn't exit. Return True if gone.

    Re-verifies the pid is still a `claude` process immediately before each signal:
    between the caller's liveness snapshot and this call the holder may have exited and
    the OS may have recycled the pid onto an unrelated process. Re-checking shrinks that
    race to the syscall boundary (the residual window is irreducible with pids). If the
    pid no longer looks like a claude holder, the original is gone — the lock is already
    freed — so we report success without signalling.
    """
    if "claude" not in pid_command(pid):
        return True  # original holder gone (or pid recycled) — lock already freed
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True  # already gone
    deadline = time.monotonic() + TERM_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(TERM_POLL_SEC)
    if "claude" not in pid_command(pid):
        return True  # exited and pid recycled during the wait — don't SIGKILL a stranger
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    time.sleep(TERM_POLL_SEC)
    return not pid_alive(pid)


def write_cheatsheet(targets: list[Holder]) -> None:
    CHEATSHEET.parent.mkdir(parents=True, exist_ok=True)
    body = "\n\n".join(resume_command(h) for h in targets)
    CHEATSHEET.write_text(f"# Resume the sessions freed for the glasses:\n\n{body}\n")


def freed_state_payload(
    targets: list[Holder],
    freed_at: float,
    panes: Mapping[str, str | None],
) -> dict[str, object]:
    """The JSON handoff resume-sessions.py reads back after the hike.

    Each session carries the tmux ``pane`` it was freed from (or ``null`` if it wasn't
    in tmux), so the resume side can send ``claude --resume`` back into that same pane.
    """
    return {
        "freed_at": freed_at,
        "sessions": [
            {
                "session_id": h.session_id,
                "cwd": h.cwd,
                "name": h.name,
                "pane": panes.get(h.session_id),
            }
            for h in targets
        ],
    }


def write_freed_state(
    targets: list[Holder], freed_at: float, panes: Mapping[str, str | None]
) -> None:
    FREED_STATE.parent.mkdir(parents=True, exist_ok=True)
    FREED_STATE.write_text(
        json.dumps(freed_state_payload(targets, freed_at, panes), indent=2) + "\n"
    )


def main() -> None:
    args = sys.argv[1:]
    do_free = "--free" in args
    keep: set[str] = set()
    it = iter(args)
    for arg in it:
        if arg == "--free":
            continue
        if arg == "--keep":
            value = next(it, None)
            if value is None or value.startswith("--"):
                sys.exit("ERROR: --keep needs a session id or name.")
            keep.add(value)
        else:
            sys.exit(f"ERROR: unknown argument {arg!r}. Use --free and/or --keep NAME_OR_ID.")

    if not SESSIONS_DIR.is_dir():
        sys.exit(f"ERROR: {SESSIONS_DIR} not found (is Claude Code installed?)")

    holders = read_registrations(SESSIONS_DIR)
    alive_pids = frozenset(
        h.pid for h in holders if pid_alive(h.pid) and "claude" in pid_command(h.pid)
    )
    ancestors = ancestor_pids(os.getpid())
    self_session_id = find_self_session(SESSIONS_DIR, ancestors)
    protected_pids = frozenset(ancestors) | {os.getpid()}
    targets = select_targets(holders, self_session_id, frozenset(keep), alive_pids, protected_pids)

    if not targets:
        print("No laptop-held sessions to free — the glasses can resume any of them.")
        return

    # Resolve each target's tmux pane now, while its pid is still alive: terminate()
    # removes the process whose ancestry we walk, so capturing after freeing would find
    # nothing. Sessions not in a tmux pane map to None and resume in a fresh window.
    pane_pids = tmux_pane_pids()
    panes = {h.session_id: pane_for_pid(h.pid, pane_pids, ancestor_pids) for h in targets}

    print(f"{len(targets)} session(s) held open by a laptop `claude` process:\n")
    for h in targets:
        print(resume_command(h))
        print()
    write_cheatsheet(targets)
    print(f"Resume cheatsheet written to {CHEATSHEET}")

    if not do_free:
        print("\n(dry run — re-run with --free to terminate these holders and free the locks.)")
        return

    print("\nFreeing:")
    for h in targets:
        ok = terminate(h.pid)
        pane = panes[h.session_id]
        where = f"pane {pane}" if pane else "no tmux pane → resume opens a fresh window"
        print(
            f"  {'freed ' if ok else 'FAILED'} {h.session_id}  "
            f"(pid {h.pid}, {h.name or 'unnamed'}, {where})"
        )
    write_freed_state(targets, time.time(), panes)
    print(f"\nDone. State written to {FREED_STATE} for resume-sessions.py.")
    print("The glasses can now resume these; back home, run resume-sessions.py to reopen them.")


if __name__ == "__main__":
    main()
