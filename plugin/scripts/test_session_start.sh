#!/usr/bin/env bash
# Tests session-start.sh's hook → engine invocation: both SessionStart and PreCompact inject the
# active-mission reminder via `status --brief` (no extra flags — the old `--suggest` starter menu was
# removed); worker sessions exit early and emit nothing. A fake engine (echoes its args) stands in for
# the real binary via MEDLEY_ENGINE; HOME is a throwaway dir so no real ~/.medley / ~/.claude state is
# touched, and MEDLEY_DAEMON=0 skips the detached pre-warm.
# Run: bash plugin/scripts/test_session_start.sh
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SS="$DIR/session-start.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fail=0

# Fake engine: no .cjs/.js/.mjs suffix, so session-start.sh execs it directly and we see its argv.
FAKE="$tmp/fake-engine"
printf '#!/usr/bin/env bash\necho "ENGINE_ARGS:$*"\n' > "$FAKE"
chmod +x "$FAKE"

# Pre-seed the one-time setup markers (statusline auto-wire SEED, CLI alias offer) so this session is
# offer-free and their plain-text output doesn't clutter the assertions (each has its own coverage).
mkdir -p "$tmp/.medley"
: > "$tmp/.medley/statusline-autowired"
: > "$tmp/.medley/cli-offered"

# Drive session-start.sh with a hook payload on stdin (as Claude Code delivers it).
run() { # $1 = JSON payload; empty MEDLEY_WORKER; daemon prewarm disabled
  printf '%s' "$1" | HOME="$tmp" MEDLEY_DATA_DIR="$tmp" MEDLEY_ENGINE="$FAKE" MEDLEY_DAEMON=0 MEDLEY_WORKER="" bash "$SS" 2>/dev/null
}
assert_contains() { case "$1" in *"$2"*) : ;; *) echo "FAIL [$3]: expected '$2' in output:"; echo "$1"; fail=1 ;; esac; }
assert_missing() { case "$1" in *"$2"*) echo "FAIL [$3]: did NOT expect '$2' in output:"; echo "$1"; fail=1 ;; *) : ;; esac; }

# 1. SessionStart → plain `status --brief`, never --suggest (the starter menu was removed).
out="$(run '{"hook_event_name":"SessionStart","session_id":"s1","cwd":"'"$tmp"'"}')"
assert_contains "$out" "ENGINE_ARGS:status --brief" "SessionStart briefs"
assert_missing "$out" "--suggest" "SessionStart omits --suggest"

# 2. PreCompact → plain `status --brief` too.
out="$(run '{"hook_event_name":"PreCompact","session_id":"s1","cwd":"'"$tmp"'"}')"
assert_contains "$out" "ENGINE_ARGS:status --brief" "PreCompact briefs"
assert_missing "$out" "--suggest" "PreCompact omits --suggest"

# 3. Worker sessions exit early — never brief.
out="$(printf '%s' '{"hook_event_name":"SessionStart"}' | HOME="$tmp" MEDLEY_DATA_DIR="$tmp" MEDLEY_ENGINE="$FAKE" MEDLEY_DAEMON=0 MEDLEY_WORKER=1 bash "$SS" 2>/dev/null)"
assert_missing "$out" "ENGINE_ARGS" "worker emits nothing"

if [ "$fail" = 0 ]; then echo "ok: session-start hook → status --brief mapping"; else exit 1; fi
