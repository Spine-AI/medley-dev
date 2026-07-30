#!/usr/bin/env bash
# medley — COMPLETE uninstall. `/plugin uninstall medley` only unregisters the plugin; it leaves the
# shared launchd daemon, ~/.medley (mission DB + history), the downloaded engine binaries, the plugin
# cache/marketplace clones, and any pf/hosts/shell edits behind. This removes ALL of it.
#
# It is self-sufficient: it does every step itself (launchd, hosts, pf, dirs, shell, settings) rather
# than relying on the engine binary — so it still works when the binary is missing or crash-looping.
# The engine is only asked for a graceful `service stop` if it happens to be runnable.
#
# Usage:
#   uninstall.sh            interactive — shows the plan, asks before removing (and again before sudo)
#   uninstall.sh -y|--yes   non-interactive — remove everything, no prompts
#   uninstall.sh -n|--dry-run   print the plan and exit; touch nothing
#   uninstall.sh --keep-data    keep ONLY the irreplaceable data — the mission DB, config.toml and any
#                               BYOK keys. The daemon and every cached binary go regardless: the daemon
#                               is part of the plugin, and a reinstall re-downloads the rest.
#   uninstall.sh -h|--help
#
# macOS only (Medley is macOS-only). Fail-soft: a step that can't complete is reported, not fatal.
#
# NOTE ON ORDERING: run THIS FIRST, then the host's own `/plugin uninstall medley` /
# `codex plugin remove`. The reverse order deletes the plugin cache this script lives in.
set -u

YES=0; DRY_RUN=0; KEEP_DATA=0
for a in "$@"; do
  case "$a" in
    -y|--yes)        YES=1 ;;
    -n|--dry-run)    DRY_RUN=1 ;;
    --keep-data)     KEEP_DATA=1 ;;
    -h|--help)
      awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
      exit 0 ;;
    *) echo "uninstall.sh: unknown option '$a' (see --help)" >&2; exit 2 ;;
  esac
done

if [ "$(uname -s)" != "Darwin" ]; then
  echo "medley uninstall: Medley is macOS-only; nothing to remove on $(uname -s)." >&2
  exit 0
fi

# ── Constants (must match the engine's launchd.ts / domain-setup.ts / session-start.sh) ────────────
LABEL_DAEMON="ai.getmedley.daemon"
LABEL_SHIPIT="ai.getmedley.medley.ShipIt"   # legacy Squirrel/Electron-era updater
PF_PLIST="/Library/LaunchDaemons/ai.getmedley.pf.plist"
PF_CONF="/etc/pf-medley.conf"
PF_ANCHOR="/etc/pf.anchors/ai.getmedley"
HOSTS_BEGIN="# >>> medley dashboard >>>"
HOSTS_END="# <<< medley dashboard <<<"
CLI_BEGIN="# >>> medley cli >>>"
CLI_END="# <<< medley cli <<<"

# This script's own dir, so it can call its siblings (strip-codex-config.py). Self-located rather
# than taken from the environment: the uninstaller is often run directly from a shell, where no
# ${CLAUDE_PLUGIN_ROOT} exists.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LA_DIR="$HOME/Library/LaunchAgents"
MEDLEY_DIR="$HOME/.medley"
SETTINGS="$HOME/.claude/settings.json"

# This is the DEV-CHANNEL uninstaller, so every CHANNEL-SCOPED path below carries the dev marketplace
# name. Both hosts derive their dirs as <plugin>-<marketplace> / <marketplace>, so one constant drives
# them all. Names are enumerated EXPLICITLY — a bare `medley*` glob would eat a co-installed STABLE
# install (or an unrelated plugin), and the two channels are meant to be independently removable.
#
# The plugin-data dirs are the one exception: they hold the DAEMON's binaries, and the daemon is
# SHARED — one launchd job, one ~/.medley, whichever channel installed it. Leaving another channel's
# binary behind after tearing that daemon down would strand 80MB pointing at nothing, so those dirs
# are discovered by ownership (medley_owned_data_dirs) across every host and channel rather than
# derived from MARKET.
MARKET="medley-dev"
CC_CACHE="$HOME/.claude/plugins/cache/$MARKET"
CC_MARKET="$HOME/.claude/plugins/marketplaces/$MARKET"
# Codex: `codex plugin add` copies into ~/.codex/plugins/cache and records the install in config.toml
# (it has no marketplaces DIR — that is Claude Code only).
CX_CACHE="$HOME/.codex/plugins/cache/$MARKET"
CX_CONFIG="$HOME/.codex/config.toml"
# Codex records the install across three table families in config.toml, all keyed by plugin@market.
CX_PLUGIN_KEY="medley@$MARKET"

uid="$(id -u)"

# ── helpers ────────────────────────────────────────────────────────────────────────────────────────
say()  { printf '%s\n' "$*"; }
note() { printf '  %s\n' "$*"; }
act()  { if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else printf '  + %s\n' "$*"; fi; }

# ── the purge list ─────────────────────────────────────────────────────────────────────────────────
# The engine owns the canonical classification of what an uninstall removes (engine/services/
# purge-plan.ts). When its binary resolves we ask it — one source of truth, and it knows about paths
# added after this script shipped. When it does NOT (missing, crash-looping, wrong arch: exactly the
# cases this script exists for) we fall back to the hand-kept list below. A unit test in the engine
# repo pins the two together, so the fallback cannot silently rot.
#
# Paths are relative to ~/.medley. Everything OUTSIDE it — the hosts' plugin-data dirs — is discovered
# by medley_owned_data_dirs() instead, since those live under two different host roots.
#
# >>> purge-plan fallback: DAEMON (removed even with --keep-data) >>>
medley_fallback_daemon() {
  cat <<'PATHS'
bin/medley-daemon
bin/medley-engine
bin/medley-engine.src
state/engine.pid
state/daemon.lock
state/daemon.log
state/engine.log
state/dashboard.json
state/.rolling
PATHS
}
# <<< purge-plan fallback: DAEMON <<<
# >>> purge-plan fallback: REGENERABLE (rebuilt by the next session) >>>
medley_fallback_regenerable() {
  cat <<'PATHS'
bin/medley-mcp
bin/medley-gateway
statusline.sh
engine-path
codex-engine-pin
codex-plugin-data
engine-override
statusline-autowired
statusline-offered
cli-offered
config.schema.json
app-runs
browser-previews
state/config.schema.json
state/mcp-token
state/plugin-pin
state/sl-cache
state/openrouter-models.json
state/update.json
state/app-runs
Cache
Code Cache
GPUCache
DawnGraphiteCache
DawnWebGPUCache
Local Storage
Session Storage
Shared Dictionary
blob_storage
SharedStorage
DIPS
Preferences
Network Persistent State
Trust Tokens
Trust Tokens-journal
.updaterId
PATHS
}
# <<< purge-plan fallback: REGENERABLE <<<

# Every plugin-data dir on this machine holding one of OUR downloaded engine binaries — both hosts,
# every channel. Ownership is PROVEN by finding a `bin/medley-engine-*` inside rather than assumed
# from the directory name: a dir is `<plugin>-<marketplace>`, so a third-party `medley-foo@bar` would
# match a naive `medley-*` glob. Only ensure-engine.sh ever puts a binary there.
medley_owned_data_dirs() {
  for root in "$HOME/.claude/plugins/data" "$HOME/.codex/plugins/data"; do
    [ -d "$root" ] || continue
    for d in "$root"/medley-*; do
      [ -d "$d" ] || continue
      if [ -n "$(find "$d/bin" -maxdepth 1 -name 'medley-engine-*' -print -quit 2>/dev/null)" ]; then
        printf '%s\n' "$d"
      fi
    done
  done
}

# The paths to remove, one per line. Uses the engine's own plan (probed once into
# $ENGINE_PURGE_PLAN — see below) when there is one; falls back to the lists above otherwise.
# `--keep-data` drops nothing from this — the engine's `keep` rows are never emitted here, and the
# fallback lists contain no data paths by construction.
medley_purge_paths() {
  if [ -n "$ENGINE_PURGE_PLAN" ]; then
    printf '%s\n' "$ENGINE_PURGE_PLAN" | awk -F'\t' '$1 == "daemon" || $1 == "regenerable" { print $2 }'
    return 0
  fi
  # Existence-filtered like the engine's own plan, so the count the user is shown is the count that
  # will actually be removed either way (rm_path skips a missing path silently, so an unfiltered list
  # made the fallback plan overstate by ~15 paths).
  { medley_fallback_daemon; medley_fallback_regenerable; } | while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    [ -e "$MEDLEY_DIR/$rel" ] && printf '%s\n' "$MEDLEY_DIR/$rel"
  done
  # The engine's own pre-migration snapshots — a full copy of the state dir each, so the largest item
  # here after the binaries. Globbed rather than listed, matching purge-plan.ts's readdir expansion.
  for d in "$MEDLEY_DIR"/state.backup-*; do
    [ -e "$d" ] && printf '%s\n' "$d"
  done
  medley_owned_data_dirs
  # $TMPDIR session markers (session-start.sh's continuation marker, edit-conflict-gate.py's
  # warn-once dirs). Exact prefixes only — never a bare `medley-*` sweep of a shared temp dir.
  find "${TMPDIR:-/tmp}" -maxdepth 1 \( -name 'medley-continuation-*' -o -name 'medley-warned-*' \) 2>/dev/null || true
}

confirm() { # $1 = prompt. Honors -y; aborts if no TTY and not -y.
  [ "$YES" = 1 ] && return 0
  [ "$DRY_RUN" = 1 ] && return 0
  if [ ! -t 0 ] && [ ! -r /dev/tty ]; then
    echo "medley uninstall: not a terminal — re-run with --yes to proceed non-interactively." >&2
    exit 1
  fi
  printf '%s [y/N] ' "$1" > /dev/tty
  local reply=""; read -r reply < /dev/tty || true
  case "$reply" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

launchd_present() { launchctl print "gui/${uid}/$1" >/dev/null 2>&1 || [ -e "$LA_DIR/$1.plist" ]; }
bootout()         { launchctl bootout "gui/${uid}/$1" >/dev/null 2>&1 || true; }

rm_path() { # $1 = path; refuses empty / "/" / "$HOME"
  local p="$1"
  if [ -z "$p" ] || [ "$p" = "/" ] || [ "$p" = "$HOME" ]; then
    note "refusing to remove unsafe path '$p'"
    return 0
  fi
  [ -e "$p" ] || return 0
  if [ "$DRY_RUN" = 1 ]; then
    act "rm -rf $p"
  elif rm -rf "$p"; then
    note "removed $p"
  else
    note "could not remove $p"
  fi
}

# ── resolve the engine binary (for a graceful stop + the purge plan; we can do everything ourselves) ─
# A local dev build is a .cjs and needs `node`; a shipped engine is a self-contained binary. Run either
# through medley_engine_run so callers don't have to care.
ENGINE=""
for c in "${MEDLEY_ENGINE:-}" \
         "$([ -f "$MEDLEY_DIR/engine-override" ] && cat "$MEDLEY_DIR/engine-override" 2>/dev/null)" \
         "$([ -f "$MEDLEY_DIR/engine-path" ] && cat "$MEDLEY_DIR/engine-path" 2>/dev/null)" \
         "$MEDLEY_DIR/bin/medley-engine"; do
  [ -n "$c" ] || continue
  # A dev build is a .cjs run through `node`, so readable is enough — requiring +x made the two dev
  # overrides silently lose to whatever binary happened to be installed.
  case "$c" in
    *.cjs|*.js|*.mjs) if [ -f "$c" ] && command -v node >/dev/null 2>&1; then ENGINE="$c"; break; fi ;;
    *)                if [ -x "$c" ]; then ENGINE="$c"; break; fi ;;
  esac
done
if [ -z "$ENGINE" ]; then
  # Any host, any channel may hold the binary — a Codex-only machine has it under ~/.codex, and a dev
  # install under a `-dev`/`-inline` suffixed dir. Enumerated by the same ownership rule as the purge.
  while IFS= read -r d; do
    [ -n "$d" ] || continue
    ENGINE="$(find "$d/bin" -maxdepth 1 -type f -name 'medley-engine-*' -perm -u+x 2>/dev/null | head -1)"
    [ -n "$ENGINE" ] && break
  done <<EOF
$(medley_owned_data_dirs)
EOF
fi

medley_engine_run() { # "$@" = engine args; routes a dev .cjs through node
  case "$ENGINE" in
    *.cjs|*.js|*.mjs) node "$ENGINE" "$@" ;;
    *)                "$ENGINE" "$@" ;;
  esac
}

# Probe the engine's canonical purge plan ONCE, at top level. Deliberately not inside
# medley_purge_paths: that runs in a command substitution, so a "did the engine answer?" flag set there
# would be assigned in a subshell and never reach the plan below. Empty ⇒ no engine, or one older than
# `service purge-plan` ⇒ use the fallback lists.
ENGINE_PURGE_PLAN=""
if [ -n "$ENGINE" ]; then
  ENGINE_PURGE_PLAN="$(medley_engine_run service purge-plan --paths 2>/dev/null)" || ENGINE_PURGE_PLAN=""
fi

# ── detect what actually exists, so the plan is honest and sudo is only used when needed ───────────
hosts_has_block=0; grep -qF "$HOSTS_BEGIN" /etc/hosts 2>/dev/null && hosts_has_block=1
pf_present=0; { [ -e "$PF_PLIST" ] || [ -e "$PF_CONF" ] || [ -e "$PF_ANCHOR" ]; } && pf_present=1
need_sudo=0; { [ "$hosts_has_block" = 1 ] || [ "$pf_present" = 1 ]; } && need_sudo=1

stale_agents="$(find "$LA_DIR" -maxdepth 1 -name 'ai.getmedley.daemon.*.plist' 2>/dev/null || true)"

# Detection must be as PRECISE as strip-codex-config.py's is_ours(), which requires the closing quote
# after the plugin key. A bare `grep -F medley@medley` is satisfied by the DEV channel's
# `medley@medley-dev`, so the stable uninstaller announced "will remove medley entries from
# config.toml" on a machine where only the dev channel existed — then removed nothing, because the
# stripper is precise. The plan must not promise what the action won't do.
cx_config_has_blocks=0
if [ -f "$CX_CONFIG" ]; then
  if grep -qF "[plugins.\"$CX_PLUGIN_KEY\"" "$CX_CONFIG" 2>/dev/null \
    || grep -qF "[marketplaces.$MARKET]" "$CX_CONFIG" 2>/dev/null \
    || grep -qF "[hooks.state.\"$CX_PLUGIN_KEY:" "$CX_CONFIG" 2>/dev/null; then
    cx_config_has_blocks=1
  fi
fi

# Is Medley doing work RIGHT NOW? The daemon persists workers as paused on a graceful stop, but an
# uninstall is not a pause — warn before destroying the state a live mission is mid-write on.
#
# Read from the daemon's unauthenticated /healthz `busy` flag (= !isDaemonIdle(): live workers, a review
# in flight, or a mission starting) rather than `status --brief`, which is REPO-scoped — run from any
# other directory it reports nothing, so it silently missed a mission running in a different repo. This
# needs no engine binary, which is the whole point of this script.
daemon_busy=0
daemon_port="$(sed -n 's/.*"port"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' "$MEDLEY_DIR/state/dashboard.json" 2>/dev/null | head -1)"
if [ -n "$daemon_port" ] && command -v curl >/dev/null 2>&1; then
  if curl -fsS --max-time 2 "http://127.0.0.1:$daemon_port/healthz" 2>/dev/null | grep -q '"busy":[[:space:]]*true'; then
    daemon_busy=1
  fi
fi

# ── plan ───────────────────────────────────────────────────────────────────────────────────────────
# Resolved once: the plan and the removal step must describe the same set of paths.
purge_list="$(medley_purge_paths)"
purge_count="$(printf '%s' "$purge_list" | grep -c . || true)"
# How to show the user the exact list themselves (a dev .cjs needs `node` in front). Only offered when
# the engine actually answered — an engine older than `service purge-plan` would just print a usage error.
purge_plan_cmd=""
if [ -n "$ENGINE_PURGE_PLAN" ]; then
  case "$ENGINE" in
    *.cjs|*.js|*.mjs) purge_plan_cmd="node $ENGINE service purge-plan" ;;
    *)                purge_plan_cmd="$ENGINE service purge-plan" ;;
  esac
fi

say ""
say "medley — complete uninstall${DRY_RUN:+ (dry run)}"
if [ "$daemon_busy" = 1 ]; then
  say ""
  say "⚠️  Medley is BUSY right now — a mission has live workers, or a review is in flight."
  say "    Uninstalling stops them mid-task and (unless --keep-data) deletes their state."
  say "    Consider letting it finish first: open the dashboard, or run \`mission_wait\`."
fi
say "This will remove:"
say "  • the shared daemon — its LaunchAgent, the launcher, the TCC-stable exec link, and every"
say "    downloaded engine binary on every host and channel"
launchd_present "$LABEL_DAEMON" && say "  • LaunchAgent $LABEL_DAEMON"
launchd_present "$LABEL_SHIPIT" && say "  • legacy LaunchAgent $LABEL_SHIPIT (Squirrel updater)"
[ -n "$stale_agents" ] && say "  • stale per-repo LaunchAgents ($(printf '%s' "$stale_agents" | wc -l | tr -d ' ') found)"
[ "$need_sudo" = 1 ] && say "  • dashboard.medley /etc/hosts entry + pf redirect  (needs sudo)"
say "  • $purge_count daemon + regenerable path(s) under $MEDLEY_DIR and the hosts' plugin-data dirs"
[ -n "$purge_plan_cmd" ] && say "    (see the exact list:  $purge_plan_cmd)"
while IFS= read -r d; do
  [ -n "$d" ] && say "      ↳ $d   (downloaded engine binaries)"
done <<EOF
$(medley_owned_data_dirs)
EOF
if [ "$KEEP_DATA" = 1 ]; then
  say "  • KEEPING your data per --keep-data: $MEDLEY_DIR/state/medley.db (missions + history),"
  say "    config.toml (providers, routing) and any BYOK *.key files. The daemon still goes."
else
  [ -d "$MEDLEY_DIR" ] && say "  • ALL of $MEDLEY_DIR — including mission history, config.toml and BYOK keys"
fi
[ -d "$CC_CACHE" ]  && say "  • $CC_CACHE   (Claude Code: cached plugin versions)"
[ -d "$CC_MARKET" ] && say "  • $CC_MARKET   (Claude Code: marketplace clone)"
[ -d "$CX_CACHE" ]  && say "  • $CX_CACHE   (Codex: cached plugin versions)"
[ "$cx_config_has_blocks" = 1 ] && say "  • medley entries in $CX_CONFIG   (plugin, marketplace, hook-trust)"
grep -qF "$CLI_BEGIN" "$HOME/.zshrc" 2>/dev/null && say "  • medley cli alias block in ~/.zshrc"
say ""
if [ "$KEEP_DATA" != 1 ] && [ -d "$MEDLEY_DIR" ]; then
  say "To keep your mission history, re-run with --keep-data."
  say ""
fi

if [ "$DRY_RUN" = 1 ]; then
  say "Dry run — nothing was removed. Re-run without --dry-run (or with --yes) to apply."
  exit 0
fi
confirm "Remove all of the above?" || { say "Aborted — nothing removed."; exit 0; }

# ── 1. stop the daemon + kill stragglers ───────────────────────────────────────────────────────────
# `service stop` is a GRACEFUL stop: the daemon persists in-flight workers as paused before exiting
# (worker-manager's shutdownForQuit). Give it a moment to finish that before resorting to signals —
# and give the same grace on the engine-less path, where launchd's bootout delivers the SIGTERM that
# runs the very same shutdown handler.
say ""; say "stopping the engine…"
if [ -n "$ENGINE" ]; then act "$ENGINE service stop"; medley_engine_run service stop >/dev/null 2>&1 || true; fi
bootout "$LABEL_DAEMON"   # stop launchd relaunching it while we clean up
for _ in 1 2 3 4 5 6 7 8 9 10; do
  pgrep -f 'medley-engine-[0-9]' >/dev/null 2>&1 || break
  sleep 0.5
done
pkill -f 'medley-engine-[0-9]' 2>/dev/null || true
# Any straggler still running out of a plugin-data dir. Built from the ownership sweep rather than a
# hardcoded 'plugins/data/medley-medley' pattern, which silently missed the dev channel's
# `medley-medley-dev/` and inline dev installs.
while IFS= read -r d; do
  [ -n "$d" ] && pkill -f "$d" 2>/dev/null
done <<EOF
$(medley_owned_data_dirs)
EOF
true   # a pkill that matched nothing must not fail the script

# ── 2. launchd agents ────────────────────────────────────────────────────────────────────────────
say "removing launchd agents…"
bootout "$LABEL_DAEMON";  rm_path "$LA_DIR/$LABEL_DAEMON.plist"
bootout "$LABEL_SHIPIT";  rm_path "$LA_DIR/$LABEL_SHIPIT.plist"
if [ -n "$stale_agents" ]; then
  printf '%s\n' "$stale_agents" | while IFS= read -r p; do
    [ -n "$p" ] || continue
    bootout "$(basename "$p" .plist)"; rm_path "$p"
  done
fi

# ── 3. system (sudo): /etc/hosts entry + pf redirect ───────────────────────────────────────────────
if [ "$need_sudo" = 1 ]; then
  say "removing the dashboard.medley domain + pf redirect (system files, sudo)…"
  if confirm "  Run sudo to edit /etc/hosts and remove the pf redirect?"; then
    sudo_script="$(mktemp)"
    cat > "$sudo_script" <<SUDO
set -e
if grep -qF '$HOSTS_BEGIN' /etc/hosts 2>/dev/null; then
  tmp="\$(mktemp)"
  awk 'index(\$0,"$HOSTS_BEGIN"){s=1} !s{print} index(\$0,"$HOSTS_END"){s=0}' /etc/hosts > "\$tmp"
  cat "\$tmp" > /etc/hosts && rm -f "\$tmp"
  echo "  stripped dashboard.medley from /etc/hosts"
fi
launchctl bootout system '$PF_PLIST' 2>/dev/null || true
for f in '$PF_PLIST' '$PF_CONF' '$PF_ANCHOR'; do [ -e "\$f" ] && { rm -f "\$f"; echo "  removed \$f"; }; done
pfctl -f /etc/pf.conf 2>/dev/null || true
SUDO
    sudo bash "$sudo_script" || note "sudo step failed — /etc/hosts + pf may need manual cleanup"
    rm -f "$sudo_script"
  else
    note "skipped — remove '$HOSTS_BEGIN…$HOSTS_END' from /etc/hosts and $PF_PLIST/$PF_CONF/$PF_ANCHOR by hand"
  fi
fi

# ── 4. daemon + regenerable artifacts, then (optionally) the data ──────────────────────────────────
# The daemon and everything a reinstall rebuilds go FIRST and unconditionally — `--keep-data` protects
# a user's history, not the 80MB binary, the launcher or the TCC-stable exec link. Before this, a
# --keep-data uninstall left the entire daemon behind under ~/.medley/bin.
say "removing the daemon + regenerable files…"
while IFS= read -r p; do
  [ -n "$p" ] || continue
  case "$p" in "$MEDLEY_DIR"/*|"$HOME"/.claude/plugins/data/*|"$HOME"/.codex/plugins/data/*|"${TMPDIR:-/tmp}"*) ;;
    *) note "skipping unexpected purge path '$p'"; continue ;;
  esac
  rm_path "$p"
done <<EOF
$purge_list
EOF
if [ "$KEEP_DATA" = 1 ]; then
  note "kept $MEDLEY_DIR/state — mission DB, config.toml and BYOK keys (per --keep-data)"
else
  rm_path "$MEDLEY_DIR"
fi
# Host-owned caches. Not part of the purge list on purpose: the daemon must never touch a running
# host's registry or cache, so only this script — which the user ran deliberately — removes them.
rm_path "$CC_CACHE"
rm_path "$CC_MARKET"
rm_path "$CX_CACHE"

# ── 4b. Codex config.toml: drop the three medley table families ─────────────────────────────────────
# `codex plugin add` writes [plugins."medley@<market>"] (plus dotted sub-tables for per-tool approval
# modes), [marketplaces.<market>], and one [hooks.state."medley@<market>:hooks/hooks.json:<event>:i:j"]
# per hook event. None of it is removed by `codex plugin remove` on an already-deleted source, and a
# stale hooks.state entry keeps a trust hash for a plugin that no longer exists.
if [ "$cx_config_has_blocks" = 1 ]; then
  if command -v python3 >/dev/null 2>&1 && [ -f "$DIR/strip-codex-config.py" ]; then
    act "strip medley tables from $CX_CONFIG"
    python3 "$DIR/strip-codex-config.py" "$CX_CONFIG" "$CX_PLUGIN_KEY" "$MARKET" \
      || note "could not rewrite $CX_CONFIG — remove the medley tables by hand"
  else
    note "python3 not found — remove the [plugins.\"$CX_PLUGIN_KEY\"], [marketplaces.$MARKET] and [hooks.state.\"$CX_PLUGIN_KEY:…\"] tables from $CX_CONFIG by hand"
  fi
fi

# ── 5. shell alias block(s) ────────────────────────────────────────────────────────────────────────
strip_block() { # $1=file  $2=begin  $3=end
  local f="$1" b="$2" e="$3"
  [ -f "$f" ] && grep -qF "$b" "$f" 2>/dev/null || return 0
  cp "$f" "$f.medley.bak"
  awk -v b="$b" -v e="$e" 'index($0,b){s=1} !s{print} index($0,e){s=0}' "$f.medley.bak" > "$f"
  note "stripped medley block from $f (backup: $f.medley.bak)"
}
say "cleaning shell config…"
for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do strip_block "$rc" "$CLI_BEGIN" "$CLI_END"; done

# ── 6. settings.json statusLine (only if it points at medley) ──────────────────────────────────────
if [ -f "$SETTINGS" ]; then
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$SETTINGS" <<'PY' || true
import json, sys, shutil
f = sys.argv[1]
try:
    d = json.loads(open(f, encoding='utf-8').read())
except Exception:
    sys.exit(0)
sl = d.get('statusLine')
cmd = sl.get('command', '') if isinstance(sl, dict) else ''
if 'medley' not in str(cmd):
    sys.exit(0)
shutil.copyfile(f, f + '.medley.bak')
d.pop('statusLine', None)
open(f, 'w', encoding='utf-8').write(json.dumps(d, indent=2) + '\n')
print('  removed medley statusLine from %s (backup: %s.medley.bak)' % (f, f))
PY
  else
    grep -q 'medley' "$SETTINGS" 2>/dev/null && note "python3 not found — if your statusLine points at medley, remove it from $SETTINGS by hand"
  fi
fi

# ── done ─────────────────────────────────────────────────────────────────────────────────────────
say ""
say "medley: uninstalled."
[ "$KEEP_DATA" = 1 ] && say "Your mission history + config are still in $MEDLEY_DIR/state (reinstalling reads them back)."
say ""
say "One thing this script can't touch safely — the plugin's entry in a live host registry. Run these"
say "LAST (both are no-ops if the plugin is already gone), in whichever host you installed it:"
say "  → Claude Code:  /plugin uninstall medley"
say "  → Codex:        codex plugin remove medley@$MARKET"
say ""
say "Reinstall any time with:  /plugin install medley   ·   codex plugin add medley@$MARKET"
