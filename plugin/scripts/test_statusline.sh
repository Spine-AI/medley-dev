#!/usr/bin/env bash
# Tests statusline.sh's update-state fast path: the download breadcrumb (update.json, written by
# ensure-engine.sh) and the version-roll marker (.rolling, written by the engine daemon). Pure and
# file-driven — no engine binary required, since the fast path runs before engine resolution. HOME is
# pointed at a throwaway dir so resolve-engine.sh finds no cached engine (the delegate→silent cases).
# Run: bash plugin/scripts/test_statusline.sh
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SL="$DIR/statusline.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fail=0
now_ms=$(( $(date +%s) * 1000 ))

# No engine on PATH/cache (HOME=tmp), state dir = tmp. Empty stdin (statusline reads it after the fast path).
run() { HOME="$tmp" MEDLEY_DATA_DIR="$tmp" CLAUDE_PLUGIN_DATA="" MEDLEY_ENGINE="" bash "$SL" </dev/null 2>/dev/null; }
assert_contains() { case "$1" in *"$2"*) : ;; *) echo "FAIL [$3]: expected '$2' in '$1'"; fail=1 ;; esac; }
assert_empty() { [ -z "$1" ] && return; echo "FAIL [$2]: expected empty, got '$1'"; fail=1; }

# 1. fresh download breadcrumb → "downloading engine v<version>"
printf '{"state":"downloading","version":"0.4.3","since":%s}\n' "$now_ms" > "$tmp/update.json"
assert_contains "$(run)" "downloading engine v0.4.3" "fresh update.json"
rm -f "$tmp/update.json"

# 2. fresh roll marker → "updating engine"
printf '%s' "$now_ms" > "$tmp/.rolling"
assert_contains "$(run)" "updating engine" "fresh .rolling"
rm -f "$tmp/.rolling"

# 3. stale download breadcrumb (ancient since) → ignored → silent (no engine to delegate to)
printf '{"state":"downloading","version":"0.4.3","since":1}\n' > "$tmp/update.json"
assert_empty "$(run)" "stale update.json"
rm -f "$tmp/update.json"

# 4. stale roll marker (>60s) → ignored → silent
printf '%s' "$(( now_ms - 120000 ))" > "$tmp/.rolling"
assert_empty "$(run)" "stale .rolling"
rm -f "$tmp/.rolling"

# 5. nothing in flight → silent (falls through to engine delegation; none present)
assert_empty "$(run)" "idle"

# ── slow-path TTL cache ──────────────────────────────────────────────────────────────────────────
# Fake engine: counts each invocation (one line appended to $CE_COUNTER) and prints a line echoing its
# cwd, so we can assert BOTH the call-count (caching) and per-repo isolation (which repo it ran for).
# No .cjs/.js/.mjs suffix → statusline.sh runs it directly.
FAKE="$tmp/fake-engine"
# shellcheck disable=SC2016  # $CE_COUNTER/$PWD are intentionally literal — they expand when the fake engine runs
printf '#!/usr/bin/env bash\necho call >> "$CE_COUNTER"\nprintf "medley ▸ %%s" "$PWD"\n' > "$FAKE"
chmod +x "$FAKE"
EMPTY_FAKE="$tmp/fake-engine-empty"   # counts but prints nothing (to prove empty output is cached)
# shellcheck disable=SC2016  # $CE_COUNTER intentionally literal (see above)
printf '#!/usr/bin/env bash\necho call >> "$CE_COUNTER"\n' > "$EMPTY_FAKE"
chmod +x "$EMPTY_FAKE"
mkdir -p "$tmp/rA" "$tmp/rB"          # real dirs so the engine subshell can cd into them ($PWD differs)

# run one statusline render against the cache. $1=HOME/data root  $2=repo cwd  $3=TTL  $4=engine(optional)
run_cached() {
  local h="$1" cwd="$2" ttl="$3" eng="${4:-$FAKE}"
  printf '%s' '{"cwd":"'"$cwd"'"}' \
    | HOME="$h" MEDLEY_DATA_DIR="$h/state" CLAUDE_PLUGIN_DATA="" MEDLEY_ENGINE="$eng" \
      CE_COUNTER="$h/counter" MEDLEY_STATUSLINE_TTL="$ttl" bash "$SL" 2>/dev/null
}
calls() { [ -f "$1/counter" ] && { wc -l < "$1/counter" | tr -d ' '; } || echo 0; }
ceq()   { [ "$1" = "$2" ] && return 0; echo "FAIL [$3]: '$1' != '$2'"; fail=1; }
has()   { case "$1" in *"$2"*) return 0 ;; esac; echo "FAIL [$3]: '$2' not in '$1'"; fail=1; }
hasnt() { case "$1" in *"$2"*) echo "FAIL [$3]: '$2' unexpectedly in '$1'"; fail=1 ;; esac; return 0; }

# 6. HIT: two rapid calls, same repo, TTL=2 → engine runs ONCE, both outputs identical.
h="$tmp/h6"; mkdir -p "$h"
o1="$(run_cached "$h" "$tmp/rA" 2)"; o2="$(run_cached "$h" "$tmp/rA" 2)"
ceq "$(calls "$h")" "1" "cache HIT runs engine once"
ceq "$o1" "$o2" "cache HIT serves identical output"
has "$o1" "medley ▸ $tmp/rA" "cache output reflects the repo"

# 7. DISABLED (TTL=0): two calls → engine runs TWICE (no caching).
h="$tmp/h7"; mkdir -p "$h"
run_cached "$h" "$tmp/rA" 0 >/dev/null; run_cached "$h" "$tmp/rA" 0 >/dev/null
ceq "$(calls "$h")" "2" "TTL=0 disables the cache"

# 8. Empty output is cached: engine prints nothing → second call still served from cache (runs once).
h="$tmp/h8"; mkdir -p "$h"
e1="$(run_cached "$h" "$tmp/rA" 2 "$EMPTY_FAKE")"; e2="$(run_cached "$h" "$tmp/rA" 2 "$EMPTY_FAKE")"
ceq "$(calls "$h")" "1" "empty output cached (engine once)"
assert_empty "$e1" "empty output stays empty"; assert_empty "$e2" "empty cached output stays empty"

# 9. Cross-repo isolation: shared cache dir, two repos → each gets its OWN line, never the other's; and
#    re-hitting repo A after B does not re-run the engine (B never clobbered A's entry).
h="$tmp/h9"; mkdir -p "$h"
oA="$(run_cached "$h" "$tmp/rA" 2)"; oB="$(run_cached "$h" "$tmp/rB" 2)"
has   "$oA" "$tmp/rA" "repoA output is repoA"
hasnt "$oA" "$tmp/rB" "repoA output is not repoB"
has   "$oB" "$tmp/rB" "repoB output is repoB"
hasnt "$oB" "$tmp/rA" "repoB output is not repoA"
ceq "$(calls "$h")" "2" "distinct repos each miss once"
oA2="$(run_cached "$h" "$tmp/rA" 2)"   # repo A again → HIT (still 2 calls), uncontaminated
ceq "$(calls "$h")" "2" "repoA re-hit does not re-run engine"
ceq "$oA2" "$oA" "repoA re-hit uncontaminated by repoB"

# 10. Fast path wins over the cache: an in-flight update marker → engine never runs.
h="$tmp/h10"; mkdir -p "$h/state"
printf '{"state":"downloading","version":"9.9.9","since":%s}\n' "$now_ms" > "$h/state/update.json"
o10="$(run_cached "$h" "$tmp/rA" 2)"
has "$o10" "downloading engine v9.9.9" "fast path renders update state"
ceq "$(calls "$h")" "0" "fast path bypasses the engine entirely"

if [ "$fail" = 0 ]; then echo "ok: statusline update-state fast path + TTL cache"; else exit 1; fi
