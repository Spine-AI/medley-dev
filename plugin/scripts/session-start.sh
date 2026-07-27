#!/bin/bash
# SessionStart / PreCompact hook: (1) ensure the engine is installed into the plugin data dir,
# then (2) inject a 3-line "active mission" reminder so mission mode survives restarts and
# compaction. Prints nothing when no mission is active.
# Workers inherit the plugin (settingSources) — the mission-agent reminder must never reach a
# WORKER's context (it would misdirect it to orchestrate), and workers must not (re)install.
[ "$MEDLEY_WORKER" = "1" ] && exit 0
# Drain the hook payload (SessionStart / PreCompact deliver JSON on stdin). We don't need any field
# from it — the same active-mission reminder is injected on both events — but consuming stdin avoids a
# broken pipe on the delivering side.
cat >/dev/null 2>&1 || true
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$DIR/ensure-engine.sh" 2>/dev/null || true
ENGINE="$("$DIR/resolve-engine.sh" 2>/dev/null || true)"
[ -n "$ENGINE" ] || exit 0
# ~/.medley/engine-path (the cache the statusline + resolver fall back to when the plugin env is
# unset) is written ONLY by ensure-engine.sh:record_engine_path, which advances it monotonically —
# so a stale older-pin session can never downgrade it. We do NOT rewrite it here: a second,
# unguarded writer defeats that guard and lets an older session drag the pointer (and, via the
# prewarm below, the daemon) backward. Just ensure the dir exists for the marker files further down.
mkdir -p "${HOME}/.medley" 2>/dev/null || true
# Keep a STABLE copy of the statusline script at a fixed path outside the versioned plugin cache. A
# statusLine wired in settings.json can't use ${CLAUDE_PLUGIN_*}, so it must hard-code a path — and a
# path into the versioned cache dir (…/cache/medley/medley/<ver>/scripts/statusline.sh) silently
# breaks the moment that version's cache is pruned on a later /plugin update. Pointing settings.json at
# this stable copy instead means the statusline keeps working across every update. The copy resolves
# the engine via ~/.medley/engine-path when run from here (see statusline.sh). Atomic (tmp + mv);
# best-effort.
if [ -f "$DIR/statusline.sh" ]; then
  _sl_tmp="${HOME}/.medley/statusline.sh.tmp.$$"
  if cp "$DIR/statusline.sh" "$_sl_tmp" 2>/dev/null; then
    chmod +x "$_sl_tmp" 2>/dev/null || true
    mv -f "$_sl_tmp" "${HOME}/.medley/statusline.sh" 2>/dev/null || rm -f "$_sl_tmp" 2>/dev/null || true
  fi
fi
# Pre-warm the shared daemon out-of-band so it's already up (or already warming) by the time the MCP
# server (.mcp.json → run-engine.sh mcp) attaches. Otherwise a cold session pays the daemon boot +
# health poll INSIDE Claude Code's MCP init window and tools can fail to register on session 1.
# Fully detached `( … & )` so the hook never blocks; `service start` no-ops fast when a healthy
# daemon already answers. Skipped for the legacy in-process mode.
if [ "${MEDLEY_DAEMON:-}" != "0" ]; then
  case "$ENGINE" in
    *.cjs|*.js|*.mjs) ( node "$ENGINE" service start >/dev/null 2>&1 & ) ;;
    *)                ( "$ENGINE" service start >/dev/null 2>&1 & ) ;;
  esac
fi
# Everything this hook prints is PLAIN TEXT added to Claude's context (the one-time setup offers below
# and the active-mission reminder from `status --brief` alike) — no JSON control fields — so any of
# them may fire together in one session and simply concatenate.
# Auto-wire the Medley statusline into ~/.claude/settings.json. `statusLine` is a USER-scoped setting a
# plugin can't ship (plugin.json has no such field; a plugin settings.json silently ignores every key
# but `agent`/`subagentStatusLine`), so the only way it reaches every user is for this hook to write it.
# Delegated to python3, fail-soft — a missing/unparseable settings.json is left untouched, never clobbered:
#   • HEAL (every session): a statusLine already pointing at "medley" but not at the stable shim copied
#     above (e.g. a versioned plugin-cache path from before this shipped) is repointed → survives updates.
#   • SEED (once, marker-guarded): with no statusLine at all, write ours. A FOREIGN statusLine is left
#     alone. Either way the marker is set, so after the first pass we never re-add a user-removed line nor
#     fight a user's own statusline — we only keep healing an existing medley one.
# The uninstaller reverses it (it strips any `medley`-pointing statusLine). The marker name differs from
# the old `-offered` so the offer→auto switch re-seeds users who only ever saw the (now-removed) offer.
SL_MARKER="${HOME}/.medley/statusline-autowired"
SL_DESIRED="${HOME}/.medley/statusline.sh"
if [ -f "$SL_DESIRED" ] && command -v python3 >/dev/null 2>&1; then
  SL_ACTION="$(SL_MARKER="$SL_MARKER" python3 - "${HOME}/.claude/settings.json" "$SL_DESIRED" <<'PY' 2>/dev/null
import json, os, sys, tempfile
settings, desired = sys.argv[1], sys.argv[2]
marker = os.environ.get("SL_MARKER", "")
seeded_before = bool(marker) and os.path.exists(marker)
existed = os.path.exists(settings)
if existed:
    try:
        d = json.loads(open(settings, encoding="utf-8").read())
    except Exception:
        sys.exit(0)              # unreadable / not strict JSON (e.g. JSONC) — never touch it
    if not isinstance(d, dict):
        sys.exit(0)
else:
    d = {}
sl = d.get("statusLine")
cmd = sl.get("command") if isinstance(sl, dict) else None
action = "noop"
if isinstance(sl, dict) and isinstance(cmd, str) and "medley" in cmd:
    if cmd != desired:                          # ours, but a stale/cache path → heal to the stable shim
        d["statusLine"] = {"type": "command", "command": desired}
        action = "healed"
elif sl is None and not seeded_before:          # no statusLine yet, first pass → seed ours
    d["statusLine"] = {"type": "command", "command": desired}
    action = "seeded"
# else: a FOREIGN statusLine, or the user removed ours after seeding → leave it untouched
if action in ("healed", "seeded"):
    d_out = json.dumps(d, indent=2) + "\n"
    if existed:
        try:
            open(settings + ".medley.bak", "w", encoding="utf-8").write(
                open(settings, encoding="utf-8").read())
        except Exception:
            pass
    os.makedirs(os.path.dirname(settings), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(settings), prefix=".settings.medley.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(d_out)
    os.replace(tmp, settings)                    # atomic; concurrent sessions → last (identical) write wins
# Mark the SEED decision as made regardless of outcome, so seeding is one-shot (HEAL still runs each session).
if marker:
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        open(marker, "a", encoding="utf-8").close()
    except Exception:
        pass
print(action)
PY
)"
  if [ "$SL_ACTION" = "seeded" ]; then
    printf '%s\n' "[medley] Auto-configured a live-status statusline: added a statusLine entry to ~/.claude/settings.json pointing at ~/.medley/statusline.sh (a stable copy this plugin refreshes each session, so it survives updates). It shows mission progress while a mission runs. Let the user know it is set up — they can remove the statusLine block from settings.json any time, and the uninstaller clears it too."
  fi
fi
# One-time CLI offer: if the `medley-engine` command isn't on the user's PATH, offer (once) to add
# a shell alias so the advanced CLI (`service dashboard --setup`, service start/stop/logs) is
# reachable. Mirrors the statusline offer above — never auto-writes, single-shot via a marker.
CLI_MARKER="${HOME}/.medley/cli-offered"
if [ ! -e "$CLI_MARKER" ]; then
  if ! command -v medley-engine >/dev/null 2>&1; then
    cat <<'MEDLEY_CLI_OFFER'
[medley] One-time setup offer: the `medley-engine` CLI (advanced — dashboard branding via `service dashboard --setup`, plus service start/stop/restart/logs) is not on the user's PATH. Only with the user's explicit yes, append this exact block to ~/.zshrc (create the file if it doesn't exist):
# >>> medley cli >>>
alias medley-engine='"$(cat ~/.medley/engine-path)"'
# <<< medley cli <<<
Then tell them to run `source ~/.zshrc` or open a new terminal. The alias re-reads ~/.medley/engine-path on each call, so it survives engine upgrades. To remove it later, delete that marked block from ~/.zshrc. If the user is on a non-zsh shell (bash/fish), show them the same alias for their own rc. If they decline, drop it — this offer never repeats.
MEDLEY_CLI_OFFER
  fi
  touch "$CLI_MARKER" 2>/dev/null || true
fi
# Inject the active-mission reminder (empty when no mission is active) so mission mode survives
# restarts and compaction. Plain text — concatenates after any one-time setup offers printed above.
case "$ENGINE" in
  *.cjs|*.js|*.mjs) exec node "$ENGINE" status --brief 2>/dev/null ;;
  *)                exec "$ENGINE" status --brief 2>/dev/null ;;
esac
