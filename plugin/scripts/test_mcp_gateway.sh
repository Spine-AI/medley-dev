#!/usr/bin/env bash
# Tests mcp-gateway.sh's engine resolution — the PIN-STRICT rule, on both hosts.
#
# The same file runs from two locations and must resolve differently in each:
#   <plugin>/scripts/mcp-gateway.sh   → pin from $DIR/../engine/version   (Claude Code)
#   ~/.medley/bin/medley-gateway      → pin from ~/.medley/codex-engine-pin (Codex — no plugin env)
# and in BOTH it must refuse to run a binary that isn't the pinned one, because an older engine
# silently ignores `--gateway` and serves the ORCHESTRATOR under the gateway's name.
#
# Fake engines echo their argv so the exec is observable; HOME is a throwaway dir so no real
# ~/.medley state is touched.
# Run: bash plugin/scripts/test_mcp_gateway.sh
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$DIR/mcp-gateway.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fail=0

PIN="9.9.9"
DATA="$tmp/data"

# The plugin-dir copy (Claude Code layout) + the fixed-path copy (Codex layout).
mkdir -p "$tmp/plugin/scripts" "$tmp/plugin/engine" "$tmp/.medley/bin" "$DATA/bin"
printf '%s\n' "$PIN" > "$tmp/plugin/engine/version"
cp "$SRC" "$tmp/plugin/scripts/mcp-gateway.sh"
cp "$SRC" "$tmp/.medley/bin/medley-gateway"
chmod +x "$tmp/plugin/scripts/mcp-gateway.sh" "$tmp/.medley/bin/medley-gateway"

# The pinned binary, and a DIFFERENT (older) one that must never be reached.
mk_engine() { printf '#!/usr/bin/env bash\necho "GATEWAY[%s]:$*"\n' "$2" > "$1"; chmod +x "$1"; }
mk_engine "$DATA/bin/medley-engine-$PIN" "pinned"
mk_engine "$DATA/bin/medley-engine-0.0.1" "older"

# A fake `node` so the .cjs dispatch branch is observable without depending on the real one.
mkdir -p "$tmp/fakebin"
printf '#!/usr/bin/env bash\necho "NODE:$*"\n' > "$tmp/fakebin/node"
chmod +x "$tmp/fakebin/node"

# ensure-engine.sh next to the plugin-dir copy: records that the rescue fired (and does nothing else).
printf '#!/usr/bin/env bash\necho called > "%s/rescue-ran"\n' "$tmp" > "$tmp/plugin/scripts/ensure-engine.sh"
chmod +x "$tmp/plugin/scripts/ensure-engine.sh"

# Run a copy with a bare-ish env: no plugin vars at all unless a case adds them (this is what a Codex
# MCP server actually gets — HOME/PATH/etc and nothing else).
run() { # $1 = script path, rest = args
  local script="$1"; shift
  env -u CLAUDE_PLUGIN_ROOT -u CLAUDE_PLUGIN_DATA -u MEDLEY_ENGINE \
    HOME="$tmp" PATH="$tmp/fakebin:$PATH" bash "$script" "$@" 2>"$tmp/err"
}
assert_contains() { case "$1" in *"$2"*) : ;; *) echo "FAIL [$3]: expected '$2' in:"; echo "$1"; fail=1 ;; esac; }
assert_missing() { case "$1" in *"$2"*) echo "FAIL [$3]: did NOT expect '$2' in:"; echo "$1"; fail=1 ;; *) : ;; esac; }

# 1. Claude Code layout: the pin comes from the plugin dir and the data dir from $1. Junk breadcrumbs
#    are present to prove the plugin dir WINS — a CC session must never resolve through Codex's state.
printf '%s\n' "0.0.1" > "$tmp/.medley/codex-engine-pin"
printf '%s\n' "/nonexistent" > "$tmp/.medley/codex-plugin-data"
out="$(run "$tmp/plugin/scripts/mcp-gateway.sh" "$DATA")"
assert_contains "$out" "GATEWAY[pinned]:mcp --gateway" "plugin-dir pin resolves + passes --gateway"
assert_missing "$out" "GATEWAY[older]" "plugin dir wins over the codex breadcrumb"

# 1b. THE CLAUDE-PATH GUARANTEE. Every fixed-path fallback must be dead code when we were launched from
#     a plugin dir. With ALL THREE breadcrumbs pointing at a different (older) engine, the plugin pin
#     must still win — an engine-override leaking in here would hand CC's gateway an older binary, and
#     an older binary ignores `--gateway` and serves the ORCHESTRATOR under the gateway's name.
mk_engine "$tmp/stale-override" "stale-override"
printf '%s\n' "$tmp/stale-override" > "$tmp/.medley/engine-override"
printf '%s\n' "0.0.1" > "$tmp/.medley/codex-engine-pin"
printf '%s\n' "$DATA" > "$tmp/.medley/codex-plugin-data"
out="$(run "$tmp/plugin/scripts/mcp-gateway.sh" "$DATA")"
assert_contains "$out" "GATEWAY[pinned]:mcp --gateway" "plugin dir ignores all fixed-path fallbacks"
assert_missing "$out" "GATEWAY[stale-override]" "engine-override never reaches the claude path"
#     …and with NO datadir arg either, the plugin path must NOT silently borrow codex-plugin-data.
out="$(run "$tmp/plugin/scripts/mcp-gateway.sh")"
assert_missing "$out" "GATEWAY" "plugin dir with no datadir refuses rather than using the crumb"

# 1c. REGRESSION (caught by A/B against the pre-change file). An UNPINNED plugin dir — engine/version
#     unreadable, e.g. a --plugin-dir checkout mid-edit or a half-materialized cache — must REFUSE,
#     exactly as it did before the fallbacks existed. It must never fall through to the Codex
#     breadcrumbs and launch whatever that cache holds. "Which copy am I" is answered by LOCATION, not
#     by "did I find a pin", precisely so these two cases stay distinct.
mv "$tmp/plugin/engine/version" "$tmp/plugin/engine/version.hidden"
out="$(run "$tmp/plugin/scripts/mcp-gateway.sh" "$DATA")"
assert_missing "$out" "GATEWAY" "unpinned plugin dir refuses instead of using the codex breadcrumb"
assert_contains "$(cat "$tmp/err")" "needs engine v?" "unpinned plugin dir reports an unknown pin"
mv "$tmp/plugin/engine/version.hidden" "$tmp/plugin/engine/version"
rm -f "$tmp/.medley/engine-override"

# 2. Codex layout: no args, no plugin env — both halves come from the breadcrumbs.
printf '%s\n' "$PIN" > "$tmp/.medley/codex-engine-pin"
printf '%s\n' "$DATA" > "$tmp/.medley/codex-plugin-data"
out="$(run "$tmp/.medley/bin/medley-gateway")"
assert_contains "$out" "GATEWAY[pinned]:mcp --gateway" "breadcrumbs resolve the pinned binary"

# 3. A STALE pin fails closed: the named binary doesn't exist, and we must NOT limp along on the
#    newest-installed engine (~/.medley/engine-path) the way medley-mcp.sh deliberately does.
printf '%s\n' "7.7.7" > "$tmp/.medley/codex-engine-pin"
printf '%s\n' "$DATA/bin/medley-engine-0.0.1" > "$tmp/.medley/engine-path"
out="$(run "$tmp/.medley/bin/medley-gateway")"; rc=$?
[ "$rc" = 1 ] || { echo "FAIL [stale pin exits 1]: got $rc"; fail=1; }
assert_missing "$out" "GATEWAY" "stale pin executes nothing"
assert_contains "$(cat "$tmp/err")" "needs engine v7.7.7" "stale pin names the version it wanted"
rm -f "$tmp/.medley/engine-path"

# 4. No breadcrumbs at all (hook never ran / fresh install) → clean refusal that says so, and the
#    rescue is NOT attempted from ~/.medley/bin (there is no ensure-engine.sh there to call).
rm -f "$tmp/.medley/codex-engine-pin" "$tmp/.medley/codex-plugin-data" "$tmp/rescue-ran"
out="$(run "$tmp/.medley/bin/medley-gateway")"; rc=$?
[ "$rc" = 1 ] || { echo "FAIL [no breadcrumb exits 1]: got $rc"; fail=1; }
assert_contains "$(cat "$tmp/err")" "SessionStart hook has not run yet" "refusal explains the hook"
assert_missing "$(cat "$tmp/err")" "command not found" "no rescue attempted from the fixed path"
# REGRESSION: `< missing_file` failures are reported by the SHELL, so `2>/dev/null` on the command does
# NOT suppress them. Every breadcrumb read is `[ -f ]`-guarded so this ordinary first-run path emits no
# spurious diagnostics into the channel the host logs.
assert_missing "$(cat "$tmp/err")" "No such file or directory" "absent breadcrumbs emit no shell noise"
if [ -e "$tmp/rescue-ran" ]; then echo "FAIL [no rescue from fixed path]"; fail=1; fi

# 5. The plugin-dir copy DOES rescue via ensure-engine.sh when the pinned binary is missing.
rm -f "$tmp/rescue-ran"
out="$(run "$tmp/plugin/scripts/mcp-gateway.sh" "$tmp/empty-data")"
if [ ! -e "$tmp/rescue-ran" ]; then echo "FAIL [plugin dir rescues via ensure-engine.sh]"; fail=1; fi

# 6. ~/.medley/engine-override (the Codex dev loop's local-build pin) outranks the pin — otherwise a
#    dev testing a local engine gets a local mission proxy and a downloaded-binary gateway at once.
printf '%s\n' "$PIN" > "$tmp/.medley/codex-engine-pin"
printf '%s\n' "$DATA" > "$tmp/.medley/codex-plugin-data"
mk_engine "$tmp/local-build" "override"
printf '%s\n' "$tmp/local-build" > "$tmp/.medley/engine-override"
out="$(run "$tmp/.medley/bin/medley-gateway")"
assert_contains "$out" "GATEWAY[override]:mcp --gateway" "engine-override wins over the pin"

# 7. A .cjs override runs through node (dev build), not exec'd directly.
cp "$tmp/local-build" "$tmp/local-build.cjs"
printf '%s\n' "$tmp/local-build.cjs" > "$tmp/.medley/engine-override"
out="$(run "$tmp/.medley/bin/medley-gateway")"
assert_contains "$out" "NODE:$tmp/local-build.cjs mcp --gateway" ".cjs dispatches via node"
rm -f "$tmp/.medley/engine-override"

if [ "$fail" = 0 ]; then echo "ok: mcp-gateway pin-strict resolution (plugin dir + fixed path)"; else exit 1; fi
