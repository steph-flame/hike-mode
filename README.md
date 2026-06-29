# hike-mode

Supervise [Claude Code](https://claude.com/claude-code) sessions from your
[Even Realities G2](https://www.evenrealities.com/) glasses — read on the HUD,
approve with ring taps, reply by voice. Built so you can keep a long agent run
moving while you're away from the keyboard (on a hike, on a walk, on the couch).

It's a thin layer on top of [`even-terminal`](https://www.npmjs.com/package/@evenrealities/even-terminal)
(Even Realities' terminal↔glasses bridge):

- **`hike`** — one command for the whole round trip. `hike on` starts the bridge
  under `caffeinate` (so your Mac won't sleep mid-session) **and** frees your
  laptop sessions for the glasses; `hike off` stops the bridge **and** reopens the
  ones you continued; `hike status` tells you whether it's up and re-replays the
  pairing QR so you can re-scan without restarting.
- **`even-terminal-patch.py`** — patches `even-terminal` so a *resumed* session
  inherits its real model, permission mode, and working directory, and runs with
  an effectively unbounded turn limit (the defaults silently downgrade you).
- **`hike free` / `hike resume`** (the `free-sessions.py` / `resume-sessions.py`
  tools) — the session-lock workaround `on`/`off` drive for you, also runnable on
  their own: release the laptop processes holding your sessions so the glasses can
  resume them, then reopen them back home exactly where the glasses left off.

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
- `tmux` (for `hike resume`): `brew install tmux`
- The Even Realities mobile app, paired with your G2 glasses
- [Tailscale](https://tailscale.com/) — **required as shipped**: `hike on`
  passes `--tailscale`, and even-terminal refuses to start without a resolvable
  Tailscale IPv4. The bridge binds to all interfaces, so this keeps the pairing
  QR pointed at your tailnet (see [Security](#security)). On a trusted LAN you
  can drop it with `HIKE_TAILSCALE=0` instead
- Optional: iTerm2 (for auto-opening a window per resumed session)
- `python3` (the scripts are standard-library only — no venv needed)

## Install

```bash
git clone https://github.com/steph-flame/hike-mode.git
cd hike-mode
./install.sh
```

`install.sh` puts the `hike` command on your `PATH` (default `~/.local/bin`,
override with `BIN_DIR=...`), tucks its helpers into a sibling `libexec/`, and
applies the `even-terminal` patch. Re-run it after every `even-terminal` upgrade
— an upgrade overwrites the package with a fresh, unpatched copy.

## Usage

```bash
hike on        # start the bridge AND free your sessions for the glasses; scan the QR
hike status    # is it up? re-print the QR + connect URL to re-scan
# ... go for your walk; supervise from the glasses ...
hike off       # stop the bridge AND reopen the sessions you continued
```

`hike on` frees your laptop sessions (so the glasses can resume them) and `hike
off` reopens the ones the glasses continued — that's the round trip a hike is.
Skip either half with `hike on --no-free` / `hike off --no-resume`, and protect a
session from being freed with `hike on --keep my-project`.

You can also run the two halves on their own (both are safe dry-runs without their
action flag):

```bash
hike free                       # list locked sessions (safe; no changes)
hike free --free                # release them for the glasses
hike free --free --keep my-project   # protect one by name/id

hike resume                     # list continued vs untouched (no changes)
hike resume --launch            # reopen the ones the glasses continued
hike resume --launch --all      # also reopen the untouched freed ones
```

`hike resume` puts each session back where it makes sense: if it was freed
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
(which `hike on` passes) only changes which address the pairing QR advertises, it
does **not** restrict the bind. So the bridge is reachable on every interface
while it's running; safety rests on the bearer token plus a trusted network.
**Run it over Tailscale**, and run `hike off` when you're done.

## Configuration

| Env var | Default | What |
|---------|---------|------|
| `HIKE_DIR` | `~/.hike` | where the token, logs, and free/resume handoff live |
| `BIN_DIR` | `~/.local/bin` | where `install.sh` puts the `hike` command |
| `HIKE_TAILSCALE` | `1` | set to `0` to drop `--tailscale` (trusted-LAN use) |
| `HIKE_RESUME_NOTE` | (a "back at the laptop" note) | first-turn message `hike off` sends when reopening a session; set empty to send none |
| `EVEN_MODEL` / `EVEN_PERMISSION_MODE` / `EVEN_MAX_TURNS` | — | force these for the bridge |

## Tests

`make check` is the one gate — ruff, the Python tests, `shellcheck`, and the shell
tests. [CI](.github/workflows/ci.yml) runs this exact target on every push, so
local and CI never drift.

```bash
brew install bats-core shellcheck    # one-time: the shell tooling
make check                           # ruff + pytest + shellcheck + bats
```

- The Python tools are standard-library only (they run under bare `python3`); the
  *tests* use `pytest`, which `uv` installs into an isolated venv on first run —
  declared dev-only in `pyproject.toml`, never imported by the shipped tools.
- The shell helpers are covered by [bats](https://github.com/bats-core/bats-core),
  including a regression test for the `hike off` kill pattern.

Run a single layer with `make test` (Python), `make bats`, or `make shellcheck`.

## Caveats

- **It monkeypatches a third-party package.** The patch edits Even Realities'
  closed-source `even-terminal`. Re-run `install.sh` after each upgrade; the
  patch refuses to apply (rather than corrupt anything) if their code moved.
- **macOS only**, as noted above.
- The in-place resume only works for sessions you ran **inside tmux** — a plain
  terminal tab can't be targeted programmatically, so those reopen as fresh
  windows.

## Glasses-aware rendering

`install.sh` also installs a **`hud-profile` skill** (into `~/.claude/skills`) that
tells Claude to render for the heads-up display — verdict-first, ~one screen,
tap-sized questions, no wide tables (adapted from [memo-flow's `pager`
skill](https://github.com/GuillermoMurillo/memo-flow)). even-terminal's
`settingSources` already loads user skills, so a glasses session can be told *"use
the hud-profile skill"* and switch into HUD mode. On the way back, `hike off`
appends a one-line note to the resumed session (`HIKE_RESUME_NOTE`) so Claude knows
the hike is over and drops HUD mode. Run `/hud-profile` on the laptop to preview the
style.

## Roadmap

- Auto-inject *"use the hud-profile skill"* as the first glasses turn (the patch
  side), so HUD mode engages without being asked — the `hud-profile` skill that it
  triggers already ships.
- Generalize beyond macOS / iTerm.

## License

MIT — see [LICENSE](LICENSE).
