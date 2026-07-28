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
run_ss_codex() { # $1 = HOME dir, $2 = env assignment marking this a Codex host
  printf '%s' '{"hook_event_name":"SessionStart","session_id":"s","cwd":"'"$1"'"}' \
    | env "$2" HOME="$1" MEDLEY_DATA_DIR="$1" MEDLEY_ENGINE="$FAKE" MEDLEY_DAEMON=0 MEDLEY_WORKER="" \
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

# 8. CODEX via PLUGIN_ROOT: the whole autowire block is skipped — no settings.json is created, no
#    note is printed, and crucially the one-shot SEED MARKER is NOT consumed, so a later Claude Code
#    session on the same machine still gets its statusline seeded.
h="$(fresh_home)"
out="$(run_ss_codex "$h" "PLUGIN_ROOT=/x/.codex/plugins/cache/medley-dev/medley/0.8.6-dev.1")"
if [ ! -e "$h/.claude/settings.json" ]; then ok "codex (PLUGIN_ROOT): no ~/.claude/settings.json created"; else bad "codex-plugin-root" "settings.json was created"; fi
case "$out" in *"Auto-configured a live-status statusline"*) bad "codex-plugin-root-note" "statusline note leaked into a Codex session" ;; *) ok "codex (PLUGIN_ROOT): no statusline note printed" ;; esac
if [ ! -e "$h/.medley/statusline-autowired" ]; then ok "codex (PLUGIN_ROOT): seed marker not consumed"; else bad "codex-plugin-root-marker" "marker set by a Codex session"; fi

# 9. CODEX via the data-dir path signal alone (PLUGIN_ROOT unset — guards against an upstream rename
#    of that variable). Same expectations.
h="$(fresh_home)"
out="$(run_ss_codex "$h" "CLAUDE_PLUGIN_DATA=$h/.codex/plugins/data/medley-medley-dev")"
if [ ! -e "$h/.claude/settings.json" ]; then ok "codex (data dir): no ~/.claude/settings.json created"; else bad "codex-datadir" "settings.json was created"; fi
case "$out" in *"Auto-configured a live-status statusline"*) bad "codex-datadir-note" "statusline note leaked into a Codex session" ;; *) ok "codex (data dir): no statusline note printed" ;; esac

# 10. The stable statusline shim is STILL installed on Codex — it is inert until a Claude Code
#     session wires it, and installing it means a machine that later runs Claude Code is ready.
h="$(fresh_home)"
run_ss_codex "$h" "PLUGIN_ROOT=/x/.codex/plugins/cache/medley-dev/medley/0.8.6-dev.1" >/dev/null
if [ -x "$h/.medley/statusline.sh" ]; then ok "codex: stable statusline shim still installed"; else bad "codex-shim" "shim missing"; fi

# ── Codex Stop-hook trust warning ───────────────────────────────────────────────────────────────────
# An untrusted Stop hook is skipped by Codex with NO error, which would silently cost the user mission
# supervision. session-start.sh (a hook that IS trusted) turns that into a visible line — but only
# while a mission is live, so it can never be noise.
MISSION_ENGINE="$top/fake-engine-mission"
# shellcheck disable=SC2016  # $1 must NOT expand here — it is body text of the generated stub.
printf '#!/usr/bin/env bash\n[ "$1" = "status" ] && printf "%%s\\n" "MEDLEY MISSION: \\"Ship it\\" — 1/3 tasks done."\nexit 0\n' > "$MISSION_ENGINE"
chmod +x "$MISSION_ENGINE"
run_ss_codex_engine() { # $1=HOME, $2=engine, $3=extra env
  printf '%s' '{"hook_event_name":"SessionStart","session_id":"s","cwd":"'"$1"'"}' \
    | env "$3" HOME="$1" MEDLEY_DATA_DIR="$1" MEDLEY_ENGINE="$2" MEDLEY_DAEMON=0 MEDLEY_WORKER="" \
      bash "$SS" 2>/dev/null
}
codex_env="PLUGIN_ROOT=/x/.codex/plugins/cache/medley-dev/medley/0.8.6-dev.1"

# 11. Mission live + NO stop trust entry → warn.
h="$(fresh_home)"; mkdir -p "$h/.codex"
printf '%s\n' '[hooks.state."medley@medley-dev:hooks/hooks.json:session_start:0:0"]' > "$h/.codex/config.toml"
out="$(run_ss_codex_engine "$h" "$MISSION_ENGINE" "$codex_env")"
case "$out" in *"supervision is NOT armed"*) ok "codex: warns when the Stop hook is untrusted" ;; *) bad "stop-untrusted" "no warning printed" ;; esac
case "$out" in *"Ship it"*) ok "codex: mission reminder still injected alongside the warning" ;; *) bad "stop-untrusted-brief" "reminder lost" ;; esac

# 12. Mission live + stop trust entry PRESENT → silent (the channel is armed).
h="$(fresh_home)"; mkdir -p "$h/.codex"
printf '%s\n' '[hooks.state."medley@medley-dev:hooks/hooks.json:stop:0:0"]' > "$h/.codex/config.toml"
out="$(run_ss_codex_engine "$h" "$MISSION_ENGINE" "$codex_env")"
case "$out" in *"supervision is NOT armed"*) bad "stop-trusted" "warned even though trusted" ;; *) ok "codex: silent once the Stop hook is trusted" ;; esac

# 13. NO mission → never warn, even untrusted (this is what keeps it from being noise).
#     Needs an engine that prints NOTHING for `status --brief`, which is what the real engine does
#     with no active mission — the shared $FAKE echoes its argv and would read as "mission live".
SILENT_ENGINE="$top/fake-engine-silent"
printf '#!/usr/bin/env bash\nexit 0\n' > "$SILENT_ENGINE"; chmod +x "$SILENT_ENGINE"
h="$(fresh_home)"; mkdir -p "$h/.codex"
printf '%s\n' '[hooks.state."medley@medley-dev:hooks/hooks.json:session_start:0:0"]' > "$h/.codex/config.toml"
out="$(run_ss_codex_engine "$h" "$SILENT_ENGINE" "$codex_env")"
case "$out" in *"supervision is NOT armed"*) bad "stop-no-mission" "warned with no live mission" ;; *) ok "codex: no warning when no mission is live" ;; esac

# 14. Claude Code never warns — it has no Stop hook and its background watcher already works.
h="$(fresh_home)"; mkdir -p "$h/.codex"
printf '%s\n' '[hooks.state."medley@medley-dev:hooks/hooks.json:session_start:0:0"]' > "$h/.codex/config.toml"
out="$(run_ss "$h")"
case "$out" in *"supervision is NOT armed"*) bad "stop-claude" "Codex-only warning leaked into Claude Code" ;; *) ok "claude code: no Stop-hook warning" ;; esac

if [ "$fail" = 0 ]; then echo "ok: statusline auto-wire + codex host-gating + stop-hook trust warning"; else exit 1; fi
