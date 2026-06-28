# hike-mode

Supervise [Claude Code](https://claude.com/claude-code) sessions from your
[Even Realities G2](https://www.evenrealities.com/) glasses — read on the HUD,
approve with ring taps, reply by voice. Built so you can keep a long agent run
moving while you're away from the keyboard (on a hike, on a walk, on the couch).

It's a thin layer on top of [`even-terminal`](https://www.npmjs.com/package/@evenrealities/even-terminal)
(Even Realities' terminal↔glasses bridge):

- **`hike-on` / `hike-off`** — one command to start/stop the bridge under
  `caffeinate` (so your Mac won't sleep mid-session), with a persisted pairing
  token and the pairing QR replayed to your terminal.
- **`even-terminal-patch.py`** — patches `even-terminal` so a *resumed* session
  inherits its real model, permission mode, and working directory, and runs with
  an effectively unbounded turn limit (the defaults silently downgrade you).
- **`free-sessions.py` / `resume-sessions.py`** — the session-lock workaround:
  release the laptop processes holding your sessions so the glasses can resume
  them, then reopen them back home exactly where the glasses left off.

> **macOS only.** It leans on `caffeinate`, `tmux`, and (optionally) iTerm via
> `osascript`. PRs to generalize are welcome.

## Why it needs a patch and a lock-dance

Claude Code **locks an active session**: while a live `claude` process holds a
session on your laptop, a second `claude --resume <id>` for the same session
fails with the misleading *"no conversation found with session ID: …"*.
`even-terminal` resumes via exactly that path, so a session you left open in a
laptop tab can't be picked up from the glasses.

It doesn't have to lose anything, though — a session's whole conversation lives
in `~/.claude/projects/<bucket>/<id>.jsonl`, not in the live process. So you
*free* the laptop holder (terminating it loses nothing), let the glasses resume
it, and when you're back you `claude --resume <id>` and land where you were.
These scripts make that round trip safe and one-paste.

## Prerequisites

- macOS
- [Claude Code](https://claude.com/claude-code)
- [`even-terminal`](https://www.npmjs.com/package/@evenrealities/even-terminal):
  `npm i -g @evenrealities/even-terminal`
- `tmux` (for `resume-sessions.py`): `brew install tmux`
- The Even Realities mobile app, paired with your G2 glasses
- Recommended: [Tailscale](https://tailscale.com/) — the bridge binds to all
  interfaces, so only expose it over a trusted network (see [Security](#security))
- Optional: iTerm2 (for auto-opening a window per resumed session)
- `python3` (the scripts are standard-library only — no venv needed)

## Install

```bash
git clone https://github.com/steph-flame/hike-mode.git
cd hike-mode
./install.sh
```

`install.sh` copies `hike-on`/`hike-off` onto your `PATH` (default
`~/.local/bin`, override with `BIN_DIR=...`) and applies the `even-terminal`
patch. Re-run it after every `even-terminal` upgrade — an upgrade overwrites the
package with a fresh, unpatched copy.

## Usage

```bash
hike-on        # start the bridge; scan the printed QR with the Even app
# ... go for your walk; supervise from the glasses ...
hike-off       # stop the bridge, let the Mac sleep again
```

Before you leave, free any sessions you want to pick up on the glasses:

```bash
cd even-terminal
python3 free-sessions.py                 # list locked sessions (safe; no changes)
python3 free-sessions.py --free          # release them for the glasses
python3 free-sessions.py --free --keep my-project   # protect one by name/id
```

Back home, reopen what you continued on the glasses:

```bash
python3 resume-sessions.py               # list continued vs untouched (no changes)
python3 resume-sessions.py --launch      # reopen the ones the glasses continued
python3 resume-sessions.py --launch --all  # also reopen the untouched freed ones
```

`resume-sessions.py` puts each session back where it makes sense: if it was freed
from a **tmux pane** that's still alive, it resumes *in place* in that same pane;
otherwise it gets its own tmux session and (on iTerm) an auto-opened window, so
each lands in a separate, spread-able macOS window. `--no-open` skips the iTerm
step.

## How the patch helps

`even-terminal-patch.py` rewrites four things in `even-terminal`'s
`dist/claude/session.js` so a resumed session behaves like the one you left:

| Setting | Stock default | Patched to |
|---------|---------------|-----------|
| `model` | a hardcoded older model | the resumed session's actual model (from its transcript) |
| `permissionMode` | `acceptEdits` | the resumed session's actual mode |
| `cwd` | the project root the app sends | the dir whose Claude project *bucket* holds the transcript, so `--resume` can find it |
| `maxTurns` | `50` | effectively unbounded (`100000`) |

The `cwd` one is the subtle one: `claude --resume <id>` only finds a transcript
from the cwd whose bucket holds it, so a session that `cd`'d into a subdirectory
would otherwise fail to resume. See [`docs/internals.md`](docs/internals.md).

Override any of these per-run with `EVEN_MODEL`, `EVEN_PERMISSION_MODE`,
`EVEN_MAX_TURNS`. The patch is idempotent, backs up `session.js` before writing,
and **fails loud** if `even-terminal`'s internals have changed (re-inspect, then
re-patch) — the same way a firewall rule re-breaks after an upgrade.

## Security

`even-terminal`'s HTTP API binds `0.0.0.0` (all interfaces) — `--tailscale`
(which `hike-on` passes) only changes which address the pairing QR advertises, it
does **not** restrict the bind. So the bridge is reachable on every interface
while it's running; safety rests on the bearer token plus a trusted network.
**Run it over Tailscale**, and run `hike-off` when you're done.

## Configuration

| Env var | Default | What |
|---------|---------|------|
| `HIKE_DIR` | `~/.hike` | where the token, logs, and free/resume handoff live |
| `BIN_DIR` | `~/.local/bin` | where `install.sh` puts `hike-on`/`hike-off` |
| `EVEN_MODEL` / `EVEN_PERMISSION_MODE` / `EVEN_MAX_TURNS` | — | force these for the bridge |

## Tests

The Python tools are standard-library only; the load-bearing logic is split from
process/filesystem I/O and unit-tested:

```bash
cd even-terminal
python3 -m pytest          # or: uv run pytest
```

## Caveats

- **It monkeypatches a third-party package.** The patch edits Even Realities'
  closed-source `even-terminal`. Re-run `install.sh` after each upgrade; the
  patch refuses to apply (rather than corrupt anything) if their code moved.
- **macOS only**, as noted above.
- The in-place resume only works for sessions you ran **inside tmux** — a plain
  terminal tab can't be targeted programmatically, so those reopen as fresh
  windows.

## Roadmap

- Skills that make Claude render more glasses-friendly on the HUD (terse,
  verdict-first, tap-sized questions).
- Generalize beyond macOS / iTerm.

## License

MIT — see [LICENSE](LICENSE).
