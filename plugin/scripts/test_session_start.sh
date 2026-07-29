#!/usr/bin/env bash
# Tests session-start.sh's hook → engine invocation: both SessionStart and PreCompact inject the
# active-mission reminder via `status --brief` (never the removed `--suggest` starter menu), plus
# `--session-id <id>` and — for a reopened conversation — `--continuation`; worker sessions exit
# early and emit nothing. A fake engine (echoes its args) stands in for
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

# Drive session-start.sh with a hook payload on stdin (as Claude Code delivers it). TMPDIR is the
# throwaway dir so the continuation markers land there and never leak between runs.
run() { # $1 = JSON payload; empty MEDLEY_WORKER; daemon prewarm disabled
  printf '%s' "$1" | HOME="$tmp" TMPDIR="$tmp" MEDLEY_DATA_DIR="$tmp" MEDLEY_ENGINE="$FAKE" MEDLEY_DAEMON=0 MEDLEY_WORKER="" bash "$SS" 2>/dev/null
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

# 4. The session id is forwarded, so the reminder can tell this session's own missions from
#    another session's (supervisor vs bystander wording — host-session-bindings.ts).
out="$(run '{"hook_event_name":"SessionStart","source":"startup","session_id":"s-fresh","cwd":"'"$tmp"'"}')"
assert_contains "$out" "--session-id s-fresh" "session id forwarded"
# A brand-new session is NOT a continuation: it must never be able to claim a live mission.
assert_missing "$out" "--continuation" "startup is not a continuation"
if [ -e "$tmp/medley-continuation-s-fresh" ]; then echo "FAIL [startup writes no marker]"; fail=1; fi

# 5. A reopened conversation (`claude --resume`) comes back under a FRESH session id, so it is
#    flagged as a continuation and marked for the binder's claim check.
out="$(run '{"hook_event_name":"SessionStart","source":"resume","session_id":"s-resumed","cwd":"'"$tmp"'"}')"
assert_contains "$out" "--session-id s-resumed" "resume forwards session id"
assert_contains "$out" "--continuation" "resume is a continuation"
if [ ! -e "$tmp/medley-continuation-s-resumed" ]; then echo "FAIL [resume writes the marker]"; fail=1; fi

# 6. PreCompact keeps an earlier resume's continuation status (same conversation, marker persists).
out="$(run '{"hook_event_name":"PreCompact","session_id":"s-resumed","cwd":"'"$tmp"'"}')"
assert_contains "$out" "--continuation" "PreCompact keeps continuation"

# 7. A path-unsafe or absent session id is dropped rather than forwarded (the binder refuses to
#    write such a file either, so there is nothing for the engine to match).
out="$(run '{"hook_event_name":"SessionStart","session_id":"../escape","cwd":"'"$tmp"'"}')"
assert_missing "$out" "--session-id" "unsafe session id dropped"
out="$(run '{"hook_event_name":"SessionStart","cwd":"'"$tmp"'"}')"
assert_contains "$out" "ENGINE_ARGS:status --brief" "no session id still briefs"
assert_missing "$out" "--session-id" "absent session id dropped"

# 8. The two fixed-path launchers are installed from EITHER host (one writer; inert until a manifest
#    launches them). These exist because a Codex MCP server gets no plugin env at all.
run '{"hook_event_name":"SessionStart","session_id":"s1","cwd":"'"$tmp"'"}' >/dev/null
for shim in medley-mcp medley-gateway; do
  if [ ! -x "$tmp/.medley/bin/$shim" ]; then echo "FAIL [installs $shim]"; fail=1; fi
done
# The gateway shim is the SAME file as the plugin's own mcp-gateway.sh, not a fork.
if ! cmp -s "$DIR/mcp-gateway.sh" "$tmp/.medley/bin/medley-gateway"; then
  echo "FAIL [medley-gateway is a verbatim copy of mcp-gateway.sh]"; fail=1
fi

# 9. Claude Code must NOT write the Codex breadcrumbs: its plugin data dir may belong to a different
#    CHANNEL's plugin, and resolving the Codex gateway's pin against that cache would hand it the
#    wrong build (or miss entirely).
for crumb in codex-engine-pin codex-plugin-data; do
  if [ -e "$tmp/.medley/$crumb" ]; then echo "FAIL [claude-code writes no $crumb]"; fail=1; fi
done

# 10. Under Codex (detected from a data dir under ~/.codex/) both breadcrumbs are written, so
#     mcp-gateway.sh can keep its pin-strict resolution on a host with no plugin env.
CODEX_DATA="$tmp/.codex/plugins/data/medley-medley-dev"
mkdir -p "$CODEX_DATA"
out="$(printf '%s' '{"hook_event_name":"SessionStart","session_id":"s1","cwd":"'"$tmp"'"}' \
  | HOME="$tmp" TMPDIR="$tmp" MEDLEY_DATA_DIR="$tmp" MEDLEY_ENGINE="$FAKE" MEDLEY_DAEMON=0 MEDLEY_WORKER="" \
    CLAUDE_PLUGIN_DATA="$CODEX_DATA" bash "$SS" 2>/dev/null)"
assert_contains "$out" "ENGINE_ARGS:status --brief" "codex still briefs"
expected_pin="$(tr -d ' \t\n\r' < "$DIR/../engine/version")"
got_pin="$(tr -d ' \t\n\r' < "$tmp/.medley/codex-engine-pin" 2>/dev/null)"
[ "$got_pin" = "$expected_pin" ] || { echo "FAIL [codex pin crumb]: got '$got_pin' want '$expected_pin'"; fail=1; }
got_data="$(tr -d ' \t\n\r' < "$tmp/.medley/codex-plugin-data" 2>/dev/null)"
[ "$got_data" = "$CODEX_DATA" ] || { echo "FAIL [codex data crumb]: got '$got_data' want '$CODEX_DATA'"; fail=1; }

# 11. A WORKER exits before any of this — it must neither install a launcher nor stamp a breadcrumb
#     (it was spawned BY a live engine and must not influence which engine the gateway resolves).
rm -rf "$tmp/.medley/bin" "$tmp/.medley/codex-engine-pin" "$tmp/.medley/codex-plugin-data"
printf '%s' '{"hook_event_name":"SessionStart"}' | HOME="$tmp" MEDLEY_DATA_DIR="$tmp" MEDLEY_ENGINE="$FAKE" \
  MEDLEY_DAEMON=0 MEDLEY_WORKER=1 CLAUDE_PLUGIN_DATA="$CODEX_DATA" bash "$SS" >/dev/null 2>&1
for p in bin/medley-gateway codex-engine-pin codex-plugin-data; do
  if [ -e "$tmp/.medley/$p" ]; then echo "FAIL [worker writes no $p]"; fail=1; fi
done

if [ "$fail" = 0 ]; then echo "ok: session-start hook → status --brief mapping"; else exit 1; fi
