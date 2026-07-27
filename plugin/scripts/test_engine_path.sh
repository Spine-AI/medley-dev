#!/usr/bin/env bash
# Tests that ~/.medley/engine-path is only ever ADVANCED, never downgraded, across a SessionStart.
# Regression for the bug where a second, unguarded writer in session-start.sh let a stale older-pin
# session (a concurrent Claude Code window on a prior plugin cache) stamp the pointer backward. The
# sole writer is now ensure-engine.sh:record_engine_path (advance-only). File-driven with stub
# binaries — no engine download, no daemon (MEDLEY_DAEMON=0 skips the prewarm).
# Run: bash plugin/scripts/test_engine_path.sh
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SS="$DIR/session-start.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fail=0

DATA="$tmp/data"
BIN="$DATA/bin"
ROOT="$tmp/root"
mkdir -p "$BIN" "$ROOT/engine"

# Stub engine binaries (session-start.sh exec's `status --brief` on the resolved one; a no-op suffices).
for v in 0.4.1 0.4.5 0.4.6 0.8.5 0.8.6 0.8.6-dev.0 0.8.6-dev.1 0.8.6-dev.2 0.8.6-dev.10 0.8.7; do
  printf '#!/bin/bash\nexit 0\n' > "$BIN/medley-engine-$v"
  chmod +x "$BIN/medley-engine-$v"
done

# Run session-start.sh as a stale/normal session pinned to $1, with engine-path pre-seeded to $2
# (empty string = no pre-seed). Returns nothing; assertions read the resulting engine-path.
run_session() {
  local pin="$1" seed="$2" home="$tmp/home"
  rm -rf "$home"; mkdir -p "$home/.medley"
  [ -n "$seed" ] && printf '%s\n' "$seed" > "$home/.medley/engine-path"
  printf '%s' "$pin" > "$ROOT/engine/version"
  HOME="$home" MEDLEY_DATA_DIR="$home/.medley/state" MEDLEY_WORKER=0 MEDLEY_DAEMON=0 \
    MEDLEY_ENGINE="" CLAUDE_PLUGIN_DATA="$DATA" CLAUDE_PLUGIN_ROOT="$ROOT" \
    bash "$SS" </dev/null >/dev/null 2>&1
  cat "$home/.medley/engine-path" 2>/dev/null
}

assert_eq() { # actual expected label
  if [ "$1" = "$2" ]; then return; fi
  echo "FAIL [$3]: expected '$2', got '$1'"; fail=1
}

# 1. Stale older-pin session (0.4.1) must NOT downgrade a newer cached pointer (0.4.5).
assert_eq "$(run_session 0.4.1 "$BIN/medley-engine-0.4.5")" "$BIN/medley-engine-0.4.5" "no downgrade"

# 2. Newer-pin session (0.4.6) advances the pointer past the cached 0.4.5.
assert_eq "$(run_session 0.4.6 "$BIN/medley-engine-0.4.5")" "$BIN/medley-engine-0.4.6" "advance forward"

# 3. Equal-pin session keeps the pointer where it is.
assert_eq "$(run_session 0.4.5 "$BIN/medley-engine-0.4.5")" "$BIN/medley-engine-0.4.5" "equal stays"

# 4. Cold start (no pointer) is bootstrapped by ensure-engine.sh, not session-start.sh.
assert_eq "$(run_session 0.4.5 "")" "$BIN/medley-engine-0.4.5" "cold bootstrap"

# 5-9. Dev channel (X.Y.Z-dev.N). The shipped field-wise sort ties on every key for two dev builds
# of the same x.y.z, which both stalled the pointer (dev.1 never advanced past dev.0) and let a
# stale session stamp it backward. Ordering must match the engine's cmp() in runtime-version.ts.
assert_eq "$(run_session 0.8.6-dev.1 "$BIN/medley-engine-0.8.6-dev.0")" \
  "$BIN/medley-engine-0.8.6-dev.1" "dev.0 -> dev.1 advances"
assert_eq "$(run_session 0.8.6-dev.10 "$BIN/medley-engine-0.8.6-dev.2")" \
  "$BIN/medley-engine-0.8.6-dev.10" "dev.2 -> dev.10 advances (not lexical)"
assert_eq "$(run_session 0.8.6-dev.0 "$BIN/medley-engine-0.8.6-dev.1")" \
  "$BIN/medley-engine-0.8.6-dev.1" "stale dev.0 does not downgrade dev.1"
assert_eq "$(run_session 0.8.7 "$BIN/medley-engine-0.8.6-dev.10")" \
  "$BIN/medley-engine-0.8.7" "dev prerelease -> stable advances"
assert_eq "$(run_session 0.8.6-dev.1 "$BIN/medley-engine-0.8.5")" \
  "$BIN/medley-engine-0.8.6-dev.1" "stable -> dev prerelease advances"

# 10-11. A stable X.Y.Z and its own X.Y.Z-dev.0 are cmp-EQUAL (both parse to [X,Y,Z,0]), so >= holds
# in BOTH directions and either pin may claim the pointer. The pre-fix sort -r tiebreak made the
# shorter string always lose, so a dev -> stable switch at the same x.y.z stranded the pointer in the
# dev plugin's bin dir.
assert_eq "$(run_session 0.8.6 "$BIN/medley-engine-0.8.6-dev.0")" \
  "$BIN/medley-engine-0.8.6" "stable claims pointer from its own dev.0"
assert_eq "$(run_session 0.8.6-dev.0 "$BIN/medley-engine-0.8.6")" \
  "$BIN/medley-engine-0.8.6-dev.0" "dev.0 claims pointer from its own stable"

if [ "$fail" = 0 ]; then echo "ok: engine-path no-downgrade"; else exit 1; fi
