#!/usr/bin/env bash
# Tests session-start.sh's auto-wiring of the statusLine into ~/.claude/settings.json:
#   SEED (once) · HEAL a stale medley path (every session) · leave FOREIGN statuslines alone ·
#   respect a user-removed line after seeding · idempotence · never clobber malformed JSON.
# Each case gets a fresh HOME so marker + settings state is isolated. A fake engine (echoes argv)
# stands in via MEDLEY_ENGINE so the hook runs to completion; MEDLEY_DAEMON=0 skips the prewarm.
# Run: bash plugin/scripts/test_statusline_autowire.sh
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SS="$DIR/session-start.sh"
top="$(mktemp -d)"
trap 'rm -rf "$top"' EXIT
fail=0

FAKE="$top/fake-engine"
printf '#!/usr/bin/env bash\necho "ENGINE_ARGS:$*"\n' > "$FAKE"
chmod +x "$FAKE"

fresh_home() { local h; h="$(mktemp -d "$top/home.XXXX")"; printf '%s' "$h"; }
run_ss() { # $1 = HOME dir; drives one SessionStart
  printf '%s' '{"hook_event_name":"SessionStart","session_id":"s","cwd":"'"$1"'"}' \
    | HOME="$1" MEDLEY_DATA_DIR="$1" MEDLEY_ENGINE="$FAKE" MEDLEY_DAEMON=0 MEDLEY_WORKER="" \
      bash "$SS" 2>/dev/null
}
sl_cmd() { # $1 = settings.json path → prints statusLine.command (empty if none / not a dict)
  python3 -c 'import json,sys
try: d=json.load(open(sys.argv[1],encoding="utf-8"))
except Exception: print(""); sys.exit()
sl=d.get("statusLine"); print(sl.get("command","") if isinstance(sl,dict) else "")' "$1" 2>/dev/null
}
ok()   { echo "  ok: $1"; }
bad()  { echo "FAIL [$1]: $2"; fail=1; }
seed_settings() { mkdir -p "$1/.claude"; printf '%s' "$2" > "$1/.claude/settings.json"; }

# 1. SEED: no settings.json, no marker → statusLine written to the stable shim, note printed, marker set.
h="$(fresh_home)"
out="$(run_ss "$h")"
got="$(sl_cmd "$h/.claude/settings.json")"
if [ "$got" = "$h/.medley/statusline.sh" ]; then ok "seed writes stable shim path"; else bad "seed" "command='$got'"; fi
case "$out" in *"Auto-configured a live-status statusline"*) ok "seed prints one-time note" ;; *) bad "seed-note" "note missing from output" ;; esac
if [ -e "$h/.medley/statusline-autowired" ]; then ok "seed sets the marker"; else bad "seed-marker" "marker not created"; fi

# 2. HEAL: a statusLine on a versioned plugin-cache path → repointed to the stable shim.
h="$(fresh_home)"
seed_settings "$h" '{"statusLine":{"type":"command","command":"/x/.claude/plugins/cache/medley/medley/0.6.1/scripts/statusline.sh"}}'
run_ss "$h" >/dev/null
got="$(sl_cmd "$h/.claude/settings.json")"
if [ "$got" = "$h/.medley/statusline.sh" ]; then ok "heal repoints a stale medley path"; else bad "heal" "command='$got'"; fi

# 3. FOREIGN: a non-medley statusLine is left exactly as-is (never clobbered).
h="$(fresh_home)"
seed_settings "$h" '{"statusLine":{"type":"command","command":"/usr/local/bin/my-status.sh"}}'
run_ss "$h" >/dev/null
got="$(sl_cmd "$h/.claude/settings.json")"
if [ "$got" = "/usr/local/bin/my-status.sh" ]; then ok "foreign statusline untouched"; else bad "foreign" "command='$got'"; fi

# 4. RESPECT REMOVAL: marker already present + no statusLine → NOT re-added (user removed it on purpose).
h="$(fresh_home)"
mkdir -p "$h/.medley"; : > "$h/.medley/statusline-autowired"
mkdir -p "$h/.claude"; printf '%s' '{"model":"opus"}' > "$h/.claude/settings.json"
run_ss "$h" >/dev/null
got="$(sl_cmd "$h/.claude/settings.json")"
if [ -z "$got" ]; then ok "removed line not re-added once marker exists"; else bad "respect-removal" "command='$got'"; fi

# 5. IDEMPOTENT: already on the stable shim → no rewrite, no .medley.bak.
h="$(fresh_home)"; mkdir -p "$h/.medley"
seed_settings "$h" '{"statusLine":{"type":"command","command":"'"$h"'/.medley/statusline.sh"}}'
run_ss "$h" >/dev/null
if [ ! -e "$h/.claude/settings.json.medley.bak" ]; then ok "no-op when already correct (no backup written)"; else bad "idempotent" "unexpected .medley.bak"; fi

# 6. PRESERVE: seeding keeps sibling keys intact.
h="$(fresh_home)"
seed_settings "$h" '{"model":"opus","env":{"FOO":"bar"}}'
run_ss "$h" >/dev/null
keep="$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d.get("model",""),d.get("env",{}).get("FOO",""))' "$h/.claude/settings.json" 2>/dev/null)"
if [ "$keep" = "opus bar" ]; then ok "seed preserves sibling keys"; else bad "preserve" "siblings='$keep'"; fi

# 7. MALFORMED: invalid JSON is never touched (no crash, byte-identical after).
h="$(fresh_home)"; mkdir -p "$h/.claude"
printf '%s' '{ this is not json' > "$h/.claude/settings.json"
run_ss "$h" >/dev/null
if [ "$(cat "$h/.claude/settings.json")" = '{ this is not json' ]; then ok "malformed settings.json left intact"; else bad "malformed" "file was modified"; fi

if [ "$fail" = 0 ]; then echo "ok: statusline auto-wire (seed/heal/foreign/removal/idempotent/preserve/malformed)"; else exit 1; fi
