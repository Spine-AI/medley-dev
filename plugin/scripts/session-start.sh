#!/bin/bash
# SessionStart / PreCompact hook: (1) ensure the engine is installed into the plugin data dir,
# then (2) inject a 3-line "active mission" reminder so mission mode survives restarts and
# compaction. Prints nothing when no mission is active.
# Workers inherit the plugin (settingSources) — the mission-agent reminder must never reach a
# WORKER's context (it would misdirect it to orchestrate), and workers must not (re)install.
[ "$MEDLEY_WORKER" = "1" ] && exit 0
# Read the hook payload (SessionStart / PreCompact deliver JSON on stdin) — consuming stdin also
# avoids a broken pipe on the delivering side. Two fields matter:
#   session_id — forwarded to `status --brief` so the reminder can tell THIS session whether it is
#     the mission's supervisor or a bystander (see host-session-bindings.ts). Without it every
#     session in the repo was told "You are the mission agent. Check progress with mission_status",
#     obeyed, and got bound as a supervisor by session-mission-binder.py — self-inflicted lockdown.
#   source — "resume" means Claude Code reopened an existing conversation under a FRESH session_id,
#     so a supervisor's binding no longer matches its own id. We drop a marker that lets it re-claim
#     its mission (session-mission-binder.py reads it); a brand-new session ("startup") gets no
#     marker and so can never claim a mission another session already supervises.
MEDLEY_PAYLOAD="$(cat 2>/dev/null || true)"
medley_json_string() {
  printf '%s' "$MEDLEY_PAYLOAD" | tr -d '\n' |
    sed -n 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
}
MEDLEY_SESSION_ID="$(medley_json_string session_id)"
# Path-unsafe or absent ids are dropped, exactly like the binder refuses to write them.
case "$MEDLEY_SESSION_ID" in
  ''|*[!A-Za-z0-9._-]*) MEDLEY_SESSION_ID='' ;;
esac
if [ -n "$MEDLEY_SESSION_ID" ] && [ "$(medley_json_string source)" = "resume" ]; then
  # Same marker dir the gate uses for its warn-once markers.
  touch "${TMPDIR:-/tmp}/medley-continuation-${MEDLEY_SESSION_ID}" 2>/dev/null || true
fi
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Which host is running this hook? Codex's hook command runner injects BOTH the bare `PLUGIN_ROOT`/
# `PLUGIN_DATA` names AND the `CLAUDE_*` aliases (measured: they live in
# codex_hooks::engine::command_runner); Claude Code injects ONLY the `CLAUDE_*` pair. The data dir
# path is the second, independent signal — Codex's is under ~/.codex/plugins/data/. Either one alone
# is enough, so an upstream rename of one of them degrades to "assume claude-code", which is the
# historical behavior. Used ONLY to skip Claude-specific setup; nothing about the engine or the
# mission reminder is host-dependent.
MEDLEY_HOST="claude-code"
case "${CLAUDE_PLUGIN_DATA:-}" in
  */.codex/*) MEDLEY_HOST="codex" ;;
esac
[ -n "${PLUGIN_ROOT:-}" ] && MEDLEY_HOST="codex"
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
# Same stable-copy trick, same reason, for the MCP launcher — see scripts/medley-mcp.sh. A Codex
# plugin MCP server process gets NO plugin env (no ${PLUGIN_ROOT}, no ${CLAUDE_PLUGIN_ROOT}) and the
# Codex manifest does not interpolate one, so its `mcpServers` command cannot name the plugin dir at
# all; it addresses ~/.medley/bin/medley-mcp instead. Claude Code doesn't need this (its .mcp.json
# reaches the plugin directly), but installing it from the shared hook keeps ONE writer and means a
# machine that has ever run either host is ready for the other. This hook runs at SessionStart while
# Codex starts plugin MCP servers lazily at first tool use, so the ordering always works out.
if [ -f "$DIR/medley-mcp.sh" ]; then
  mkdir -p "${HOME}/.medley/bin" 2>/dev/null || true
  _mcp_tmp="${HOME}/.medley/bin/medley-mcp.tmp.$$"
  if cp "$DIR/medley-mcp.sh" "$_mcp_tmp" 2>/dev/null; then
    chmod +x "$_mcp_tmp" 2>/dev/null || true
    mv -f "$_mcp_tmp" "${HOME}/.medley/bin/medley-mcp" 2>/dev/null || rm -f "$_mcp_tmp" 2>/dev/null || true
  fi
fi
# Same stable-copy trick for the GATEWAY launcher (connected apps) — and note it is the SAME script as
# the plugin's own `medley_gateway` entry runs, installed at a second path rather than forked. That
# works because mcp-gateway.sh resolves its pin from `$DIR/..`: from the plugin dir that is the plugin
# root and it reads the manifest pin (Claude Code), while from ~/.medley/bin there is no engine/version
# and it falls through to the fixed-path breadcrumbs written below (Codex). One file, ONE pin-strict
# rule, two hosts — the gateway must never be routed through medley-mcp, which tracks the newest
# installed engine instead of the pin (an older binary ignores `--gateway` and would serve the
# ORCHESTRATOR under the gateway's name). Installed from either host for the same reason as medley-mcp
# above: one writer, and the copy is inert until a manifest actually launches it.
if [ -f "$DIR/mcp-gateway.sh" ]; then
  mkdir -p "${HOME}/.medley/bin" 2>/dev/null || true
  _gw_tmp="${HOME}/.medley/bin/medley-gateway.tmp.$$"
  if cp "$DIR/mcp-gateway.sh" "$_gw_tmp" 2>/dev/null; then
    chmod +x "$_gw_tmp" 2>/dev/null || true
    mv -f "$_gw_tmp" "${HOME}/.medley/bin/medley-gateway" 2>/dev/null || rm -f "$_gw_tmp" 2>/dev/null || true
  fi
fi
# CODEX ONLY: leave the two values that launcher needs at fixed paths, since a Codex MCP server gets
# neither the plugin env nor manifest interpolation. The engine PIN (a value, not a plugin root — see
# mcp-gateway.sh for why that distinction is load-bearing) and the data dir holding the downloaded
# binaries (~/.codex/plugins/data/<plugin>-<marketplace>, which is NOT versioned, so it is safe to
# store as a path). Claude Code deliberately does NOT write these: its data dir belongs to a possibly
# different CHANNEL's plugin, and resolving the Codex gateway's pin against that cache would either
# miss or hand it the wrong build. Atomic (tmp + mv), best-effort; a stale value fails closed in the
# launcher and is rewritten here on the next session start.
if [ "$MEDLEY_HOST" = "codex" ] && [ -n "${CLAUDE_PLUGIN_DATA:-}" ]; then
  medley_write_crumb() { # $1 = file name under ~/.medley, $2 = value (skipped when empty)
    [ -n "$2" ] || return 0
    _cr_tmp="${HOME}/.medley/$1.tmp.$$"
    if printf '%s\n' "$2" > "$_cr_tmp" 2>/dev/null; then
      mv -f "$_cr_tmp" "${HOME}/.medley/$1" 2>/dev/null || rm -f "$_cr_tmp" 2>/dev/null || true
    fi
  }
  if [ -f "$DIR/../engine/version" ]; then
    medley_write_crumb codex-engine-pin "$(tr -d ' \t\n\r' < "$DIR/../engine/version" 2>/dev/null)"
  fi
  medley_write_crumb codex-plugin-data "${CLAUDE_PLUGIN_DATA}"
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
# CODEX: skipped entirely. `statusLine` is a Claude Code setting; Codex 0.145 has no user-supplied
# statusline at all (`status_line` there is a TUI display pref, sitting beside status_line_use_colors
# /theme/pet — not a command hook). Running this under Codex would CREATE ~/.claude/settings.json on
# a machine that may never run Claude Code, and print a "statusline is set up, tell the user" line
# into a Codex session for something Codex cannot render. The stable copy above is still installed —
# it's inert until a Claude Code session actually wires it.
SL_MARKER="${HOME}/.medley/statusline-autowired"
SL_DESIRED="${HOME}/.medley/statusline.sh"
if [ "$MEDLEY_HOST" = "codex" ]; then
  : # no statusline on this host — see above
elif [ -f "$SL_DESIRED" ] && command -v python3 >/dev/null 2>&1; then
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
#
# CAPTURED rather than exec'd on Codex, because the untrusted-Stop-hook warning below must only fire
# when a mission is actually live (no mission ⇒ nothing to supervise ⇒ no reason to nag), and the
# reminder is exactly the "is a mission live" signal. Claude Code keeps the original exec: it has no
# Stop hook to warn about, and exec'ing saves a subshell on the hot path.
#
# `--session-id` / `--continuation` are additive: an engine older than this plugin parses only
# `--brief` and ignores the rest, falling back to the legacy repo-scoped reminder.
MEDLEY_STATUS_ARGS=(status --brief)
if [ -n "$MEDLEY_SESSION_ID" ]; then
  MEDLEY_STATUS_ARGS+=(--session-id "$MEDLEY_SESSION_ID")
  if [ -e "${TMPDIR:-/tmp}/medley-continuation-${MEDLEY_SESSION_ID}" ]; then
    MEDLEY_STATUS_ARGS+=(--continuation)
  fi
fi
if [ "$MEDLEY_HOST" != "codex" ]; then
  case "$ENGINE" in
    *.cjs|*.js|*.mjs) exec node "$ENGINE" "${MEDLEY_STATUS_ARGS[@]}" 2>/dev/null ;;
    *)                exec "$ENGINE" "${MEDLEY_STATUS_ARGS[@]}" 2>/dev/null ;;
  esac
fi

case "$ENGINE" in
  *.cjs|*.js|*.mjs) MEDLEY_BRIEF="$(node "$ENGINE" "${MEDLEY_STATUS_ARGS[@]}" 2>/dev/null || true)" ;;
  *)                MEDLEY_BRIEF="$("$ENGINE" "${MEDLEY_STATUS_ARGS[@]}" 2>/dev/null || true)" ;;
esac
printf '%s' "$MEDLEY_BRIEF"

# ── Codex: is the Stop-hook supervision channel actually armed? ─────────────────────────────────────
# On Codex the mission agent CANNOT be woken by a finished background process (no wake-on-exit), so
# supervision rides the `Stop` hook: it blocks turn-end and hands the agent the digest. That hook only
# runs if Codex has TRUSTED it, and an untrusted hook is skipped with NO error of any kind — the
# mission would simply go unsupervised and nothing would say why.
#
# Trust is granted only in an interactive session; `codex exec` never fires Stop, so it can never
# grant it, and the hash cannot be pre-seeded (it is a security control — don't). What we CAN do is
# make the silent failure loud, from a hook that IS already trusted. Fires only while a mission is
# live, so it is never noise.
if [ -n "$MEDLEY_BRIEF" ] && [ -f "${HOME}/.codex/config.toml" ]; then
  if ! grep -q 'medley@[^"]*:hooks/hooks.json:stop:' "${HOME}/.codex/config.toml" 2>/dev/null; then
    printf '\n%s\n' "[medley] ⚠️ Mission supervision is NOT armed on this host. Codex cannot wake an agent when a background process finishes, so Medley supervises through a Stop hook that hands you each progress digest — and Codex has not trusted that hook yet, so it is being skipped silently. Tell the user: approve the medley 'Stop' hook when Codex prompts (it prompts in the interactive TUI, not under \`codex exec\`); until then, digests will NOT arrive on their own and you must call mission_status yourself at each turn, or mission_wait when they ask to block until done."
  fi
fi
exit 0
