# Internals

How the pieces work, and the non-obvious decisions behind them.

## The session lock

Claude Code allows only one live process to hold a session. `even-terminal`
resumes a session by spawning its own `claude --resume <id>` (via the Agent
SDK), so while your laptop still holds that session, the glasses' resume
collides and reports the misleading *"no conversation found with session ID"*.

Nothing is lost by releasing the laptop holder, though: the entire conversation
is persisted to `~/.claude/projects/<bucket>/<id>.jsonl` continuously, not kept
in the process. `free-sessions.py` terminates the holder; the transcript stays;
`claude --resume <id>` later reloads it exactly where you were.

## The cwd-bucket gotcha (the subtle one)

Claude Code stores a transcript at `~/.claude/projects/<bucket>/<id>.jsonl`,
where `<bucket>` is the session's **creation** working directory with every
non-alphanumeric character replaced by `-`. Crucially, **`claude --resume <id>`
only finds the transcript from the cwd whose bucket holds it.**

`even-terminal` lets a resumed session inherit a working directory. The naive
choice — the session's *last* recorded cwd — breaks resume whenever the session
had `cd`'d into a subdirectory: the subdirectory encodes to a *different* bucket,
which contains no transcript, so the resume fails.

`even-terminal-patch.py` fixes this in `_ppSessionMeta`: it scans the cwds
recorded in the transcript and picks the one whose bucket-encoding matches the
directory the transcript actually lives in — never the bare last cwd. A session
that visited six subdirectories still resolves to its creation-cwd root.

## free → resume

`free-sessions.py`:

- Frees only **live, interactive (`cli`) laptop tabs**. It leaves alone: the
  session it runs under (found by walking its own ancestor pids, so running it
  *from* a glasses session is safe), background jobs, and `even-terminal`'s own
  SDK spawns. `--keep NAME_OR_ID` protects more.
- Kills only the `claude` process, not the terminal — the parent shell returns to
  its prompt, the tab/pane stays open.
- When a holder is in a **tmux pane**, it records the pane id (resolved *before*
  the kill, since terminating `claude` removes the pid whose ancestry maps it to
  a pane). It writes a human cheatsheet (`resume-cheatsheet.txt`) and a
  machine-readable handoff (`freed-sessions.json`) into `$HIKE_DIR`.

`resume-sessions.py`:

- Reads the handoff and classifies each freed session as **continued** (its
  transcript changed after the free — the glasses worked on it), **untouched**,
  or **missing**. Default reopens only continued; `--all` adds untouched.
- For each one it reopens:
  - **pane still alive** → `tmux send-keys` the `claude --resume` straight back
    into that same pane (in place).
  - **pane gone, or never in tmux** → its own new tmux session (named after the
    session, sanitized + de-duplicated) and, by default, an iTerm window opened
    and attached via `osascript`. `--no-open` skips the iTerm step.

## Why `hike-on` doesn't pin `-d`

`even-terminal`'s session-listing route scopes to its project dir
(`req.query.cwd || PROJECT_DIR`). Passing `-d <dir>` would make the glasses menu
show only sessions whose bucket is that dir and hide every other workspace.
Because the bucket-matcher patch already makes resume cwd-independent, the pin
buys nothing and costs the full session list — so `hike-on` leaves it unset.
