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
#   uninstall.sh --keep-data    keep ~/.medley (mission DB + history); remove everything else
#   uninstall.sh -h|--help
#
# macOS only (Medley is macOS-only). Fail-soft: a step that can't complete is reported, not fatal.
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

# This is the DEV-CHANNEL uninstaller, so every path below carries the dev marketplace name. Both
# hosts derive their dirs as <plugin>-<marketplace> / <marketplace>, so one constant drives all five.
# Names are enumerated EXPLICITLY — a bare `medley*` glob would eat a co-installed STABLE install (or
# an unrelated plugin), and the two channels are meant to be independently removable.
MARKET="medley-dev"
CC_DATA="$HOME/.claude/plugins/data/medley-$MARKET"
CC_CACHE="$HOME/.claude/plugins/cache/$MARKET"
CC_MARKET="$HOME/.claude/plugins/marketplaces/$MARKET"
# Codex: `codex plugin add` copies into ~/.codex/plugins/cache and gives hooks a real writable data
# dir under ~/.codex/plugins/data — which is where the downloaded engine binaries land (~96MB), the
# single biggest thing a Claude-only teardown used to leave behind.
CX_DATA="$HOME/.codex/plugins/data/medley-$MARKET"
CX_CACHE="$HOME/.codex/plugins/cache/$MARKET"
CX_CONFIG="$HOME/.codex/config.toml"
# Codex records the install across three table families in config.toml, all keyed by plugin@market.
CX_PLUGIN_KEY="medley@$MARKET"

uid="$(id -u)"

# ── helpers ────────────────────────────────────────────────────────────────────────────────────────
say()  { printf '%s\n' "$*"; }
note() { printf '  %s\n' "$*"; }
act()  { if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else printf '  + %s\n' "$*"; fi; }

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

# ── resolve the engine binary (for a graceful stop only; everything else we do ourselves) ──────────
ENGINE=""
for c in "${MEDLEY_ENGINE:-}" "$([ -f "$MEDLEY_DIR/engine-path" ] && cat "$MEDLEY_DIR/engine-path" 2>/dev/null)"; do
  [ -n "$c" ] && [ -x "$c" ] && { ENGINE="$c"; break; }
done
if [ -z "$ENGINE" ]; then
  # Either host may hold the binary — a Codex-only machine has it under ~/.codex.
  for d in "$CC_DATA/bin" "$CX_DATA/bin"; do
    [ -d "$d" ] || continue
    ENGINE="$(find "$d" -maxdepth 1 -type f -name 'medley-engine-*' -perm -u+x 2>/dev/null | head -1)"
    [ -n "$ENGINE" ] && break
  done
fi

# ── detect what actually exists, so the plan is honest and sudo is only used when needed ───────────
hosts_has_block=0; grep -qF "$HOSTS_BEGIN" /etc/hosts 2>/dev/null && hosts_has_block=1
pf_present=0; { [ -e "$PF_PLIST" ] || [ -e "$PF_CONF" ] || [ -e "$PF_ANCHOR" ]; } && pf_present=1
need_sudo=0; { [ "$hosts_has_block" = 1 ] || [ "$pf_present" = 1 ]; } && need_sudo=1

stale_agents="$(find "$LA_DIR" -maxdepth 1 -name 'ai.getmedley.daemon.*.plist' 2>/dev/null || true)"

cx_config_has_blocks=0
if [ -f "$CX_CONFIG" ] && grep -qF "$CX_PLUGIN_KEY" "$CX_CONFIG" 2>/dev/null; then cx_config_has_blocks=1; fi
if [ -f "$CX_CONFIG" ] && grep -qF "[marketplaces.$MARKET]" "$CX_CONFIG" 2>/dev/null; then cx_config_has_blocks=1; fi

# ── plan ───────────────────────────────────────────────────────────────────────────────────────────
say ""
say "medley — complete uninstall${DRY_RUN:+ (dry run)}"
say "This will remove:"
say "  • the shared daemon + any running medley-engine processes"
launchd_present "$LABEL_DAEMON" && say "  • LaunchAgent $LABEL_DAEMON"
launchd_present "$LABEL_SHIPIT" && say "  • legacy LaunchAgent $LABEL_SHIPIT (Squirrel updater)"
[ -n "$stale_agents" ] && say "  • stale per-repo LaunchAgents ($(printf '%s' "$stale_agents" | wc -l | tr -d ' ') found)"
[ "$need_sudo" = 1 ] && say "  • dashboard.medley /etc/hosts entry + pf redirect  (needs sudo)"
if [ "$KEEP_DATA" = 1 ]; then
  say "  • (keeping $MEDLEY_DIR — mission DB + history — per --keep-data)"
else
  [ -d "$MEDLEY_DIR" ] && say "  • $MEDLEY_DIR   (mission DB + history + config)"
fi
[ -d "$CC_DATA" ]   && say "  • $CC_DATA   (Claude Code: downloaded engine binaries)"
[ -d "$CC_CACHE" ]  && say "  • $CC_CACHE   (Claude Code: cached plugin versions)"
[ -d "$CC_MARKET" ] && say "  • $CC_MARKET   (Claude Code: marketplace clone)"
[ -d "$CX_DATA" ]   && say "  • $CX_DATA   (Codex: downloaded engine binaries)"
[ -d "$CX_CACHE" ]  && say "  • $CX_CACHE   (Codex: cached plugin versions)"
[ "$cx_config_has_blocks" = 1 ] && say "  • medley entries in $CX_CONFIG   (plugin, marketplace, hook-trust)"
grep -qF "$CLI_BEGIN" "$HOME/.zshrc" 2>/dev/null && say "  • medley cli alias block in ~/.zshrc"
say ""

if [ "$DRY_RUN" = 1 ]; then
  say "Dry run — nothing was removed. Re-run without --dry-run (or with --yes) to apply."
  exit 0
fi
confirm "Remove all of the above?" || { say "Aborted — nothing removed."; exit 0; }

# ── 1. stop the daemon + kill stragglers ───────────────────────────────────────────────────────────
say ""; say "stopping the engine…"
if [ -n "$ENGINE" ]; then act "$ENGINE service stop"; "$ENGINE" service stop >/dev/null 2>&1 || true; fi
bootout "$LABEL_DAEMON"   # stop launchd relaunching it while we clean up
pkill -f 'medley-engine-[0-9]'          2>/dev/null || true
# No trailing slash: the dev channel's dir is `medley-medley-dev/`, which the old
# 'plugins/data/medley-medley/' pattern silently missed. Matches both hosts and both channels.
pkill -f 'plugins/data/medley-medley'   2>/dev/null || true

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

# ── 4. data + plugin dirs ──────────────────────────────────────────────────────────────────────────
say "removing files…"
if [ "$KEEP_DATA" = 1 ]; then
  rm_path "$MEDLEY_DIR/engine-path"   # target is about to vanish; drop the stale pointer
else
  rm_path "$MEDLEY_DIR"
fi
rm_path "$CC_DATA"
rm_path "$CC_CACHE"
rm_path "$CC_MARKET"
rm_path "$CX_DATA"
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
say "One thing this script can't touch safely — the plugin's entry in a live host registry:"
[ -d "$CC_CACHE" ] || [ -d "$CC_DATA" ] || [ -f "$SETTINGS" ] && \
  say "  → Claude Code: run  /plugin uninstall medley  (a no-op if already gone)."
[ "$cx_config_has_blocks" = 1 ] || [ -d "$CX_CACHE" ] && \
  say "  → Codex: run  codex plugin remove medley@$MARKET  (a no-op if already gone)."
say "Reinstall any time with:  /plugin install medley   ·   codex plugin add medley@$MARKET"
