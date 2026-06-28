#!/usr/bin/env bats
# Behavioural tests for the shell scripts — the layer that the Python suite can't
# reach and where this project's real bugs have lived (hike-off's kill pattern).
#
# Run with:  bats test/hike.bats   (brew install bats-core)
#
# These avoid touching a real even-terminal: a fake server is a tiny script whose
# path contains `bin/even-terminal`, so its argv matches the same pattern hike-off
# greps for. Killing it exercises the exact code path that was broken.

setup() {
    REPO="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
    export HIKE_DIR="$BATS_TEST_TMPDIR/hike"
    mkdir -p "$HIKE_DIR"
    # A fake even-terminal binary: its own path carries `bin/even-terminal`, so a
    # running instance matches hike-off's SERVER_PATTERN exactly like the real one.
    FAKE_BIN="$BATS_TEST_TMPDIR/bin/even-terminal"
    mkdir -p "$(dirname "$FAKE_BIN")"
    cat > "$FAKE_BIN" <<'SH'
#!/bin/bash
# Stay alive without exec (exec would replace argv and drop `bin/even-terminal`).
while true; do sleep 1; done
SH
    chmod +x "$FAKE_BIN"
}

teardown() {
    pkill -f "$BATS_TEST_TMPDIR/bin/even-terminal" 2>/dev/null || true
}

start_fake_server() {
    "$FAKE_BIN" start &
    # Wait until it's matchable before returning.
    for _ in $(seq 1 50); do
        pgrep -f "bin/even-terminal" >/dev/null && return 0
        sleep 0.05
    done
    return 1
}

# --- the regression: hike-off must actually stop the server ---------------------

@test "hike-off stops a running server matching the pattern" {
    start_fake_server
    echo "$!" > "$HIKE_DIR/even-terminal.pid"

    run "$REPO/bin/hike-off"
    [ "$status" -eq 0 ]
    [[ "$output" == *"hike mode OFF"* ]]

    run pgrep -f "$BATS_TEST_TMPDIR/bin/even-terminal"
    [ "$status" -ne 0 ]   # gone
}

@test "hike-off on a clean machine reports OFF and succeeds" {
    run "$REPO/bin/hike-off"
    [ "$status" -eq 0 ]
    [[ "$output" == *"hike mode OFF"* ]]
}

# --- hike-status ----------------------------------------------------------------

@test "hike-status reports OFF (exit 1) when nothing is running" {
    run "$REPO/bin/hike-status"
    [ "$status" -eq 1 ]
    [[ "$output" == *"hike mode OFF"* ]]
}

@test "hike-status flags an orphan server not tracked by the pidfile" {
    start_fake_server   # running, but we deliberately write NO pidfile
    run "$REPO/bin/hike-status"
    [ "$status" -eq 1 ]
    [[ "$output" == *"server IS running"* ]]
}

# --- the hike umbrella dispatcher ----------------------------------------------

@test "hike status dispatches to the status helper (OFF when nothing runs)" {
    run "$REPO/bin/hike" status
    [ "$status" -eq 1 ]
    [[ "$output" == *"hike mode OFF"* ]]
}

@test "hike with no subcommand prints usage" {
    run "$REPO/bin/hike"
    [[ "$output" == *"umbrella command"* ]]
    [[ "$output" == *"hike on"* ]]
}

@test "hike with an unknown subcommand errors and shows usage" {
    run "$REPO/bin/hike" bogus
    [ "$status" -eq 1 ]
    [[ "$output" == *"unknown subcommand"* ]]
}

@test "hike off stops the bridge and notes nothing to reopen (no freed state)" {
    run "$REPO/bin/hike" off
    [ "$status" -eq 0 ]
    [[ "$output" == *"hike mode OFF"* ]]
    [[ "$output" == *"nothing to reopen"* ]]
}

@test "hike off --no-resume skips the reopen step" {
    run "$REPO/bin/hike" off --no-resume
    [ "$status" -eq 0 ]
    [[ "$output" == *"hike mode OFF"* ]]
    [[ "$output" != *"reopen"* ]]
}

# --- the pattern itself: it must catch the server but never caffeinate ----------

@test "SERVER_PATTERN matches both server argv forms but not the caffeinate parent" {
    # Pull the pattern straight out of hike-off so the test tracks the real source.
    pattern="$(sed -n 's/^SERVER_PATTERN="\(.*\)"$/\1/p' "$REPO/bin/hike-off")"
    [ -n "$pattern" ]

    echo "node /opt/homebrew/bin/even-terminal start --tailscale" | grep -qE "$pattern"
    echo "node /usr/local/lib/node_modules/@evenrealities/even-terminal/bin/cli.js start" | grep -qE "$pattern"
    # The caffeinate parent must NOT match — hike-off stops it via the pidfile, and
    # broadening the pattern to catch it would also catch unrelated processes.
    run bash -c "echo 'caffeinate -is even-terminal start --tailscale' | grep -qE '$pattern'"
    [ "$status" -ne 0 ]
}
