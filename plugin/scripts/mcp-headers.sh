#!/usr/bin/env bash
# headersHelper for the Medley MCP server (.mcp.json, type:http). Claude Code runs this on each
# CONNECTION and expects exactly one JSON object of header name→value on stdout, within 10s.
#
# It supplies the Bearer token for the daemon's /mcp (a stable per-user secret; the repo itself
# rides a STATIC `X-Medley-Repo-Raw: ${CLAUDE_PROJECT_DIR}` header in .mcp.json — this helper does
# NOT receive CLAUDE_PROJECT_DIR). CC CACHES this output and reuses it verbatim on reconnect (it does
# NOT re-run the helper), so the token must be STABLE and correct on the first call — including cold
# start, before the daemon's first boot. Hence read-or-create the shared token file here; the daemon
# (dashboard-server.stableToken) reads the very same file, so both agree.
#
# Fail-soft: always print a valid JSON object; never block on a download.
set -u

# Mirror the engine's userDataDir(): MEDLEY_DATA_DIR (inherited from the env when set) else the
# global default. So the token file the helper reads is the SAME one the daemon reads/creates.
STATE="${MEDLEY_DATA_DIR:-${HOME}/.medley/state}"
TOKENFILE="${STATE}/mcp-token"
PORT="${MEDLEY_DASHBOARD_PORT:-8730}"

# The plugin data dir (${CLAUDE_PLUGIN_DATA}/bin is where the engine binary is cached). CC does NOT
# reliably put CLAUDE_PLUGIN_DATA in the helper's env, but it DOES interpolate ${CLAUDE_PLUGIN_DATA}
# into the headersHelper command string — so .mcp.json passes it as $1. The cold-start bridge below
# threads it into ensure-engine.sh (which hard-requires it) so a fresh install can download the binary.
DATADIR="${1:-${CLAUDE_PLUGIN_DATA:-}}"

# The engine version THIS session's plugin is pinned to (release-managed). Sent as X-Medley-Engine-Pin
# so the daemon can detect it's serving an older engine than this session expects and roll forward
# (version handshake). CLAUDE_PLUGIN_ROOT is available to the helper; a version string is JSON-safe.
# Guard with -f first (matches resolve-engine.sh) so a missing file can't leak a redirection error.
PIN=""
VERSION_FILE="${CLAUDE_PLUGIN_ROOT:-}/engine/version"
[ -f "$VERSION_FILE" ] && PIN="$(tr -d ' \t\n\r' < "$VERSION_FILE" 2>/dev/null)"
PIN_HDR=""
[ -n "$PIN" ] && PIN_HDR=",\"X-Medley-Engine-Pin\":\"${PIN}\""

# The terminal this session runs in, sent as X-Medley-Terminal. The DAEMON has no terminal of its own
# (launchd-spawned), and this helper is the only Medley code that both runs under the user's terminal
# and talks to the daemon once per session — so it is the only place the terminal dimension on
# `session_started` can come from. TERM_PROGRAM covers most; kitty and alacritty set none, so fall
# back to their marker vars using the same token names terminal-caps.ts expects (keep the two in
# step). Value is a bare token — no version, no session id. Sanitized to [A-Za-z0-9._-] so nothing
# from the environment can break the JSON this must print.
TERMTOK="${TERM_PROGRAM:-}"
if [ -z "$TERMTOK" ]; then
  if [ -n "${KITTY_WINDOW_ID:-}" ]; then
    TERMTOK="kitty"
  elif [ -n "${ALACRITTY_SOCKET:-}${ALACRITTY_WINDOW_ID:-}" ]; then
    TERMTOK="alacritty"
  fi
fi
TERMTOK="$(printf '%s' "$TERMTOK" | tr -cd 'A-Za-z0-9._-' | cut -c1-32)"
TERM_HDR=""
[ -n "$TERMTOK" ] && TERM_HDR=",\"X-Medley-Terminal\":\"${TERMTOK}\""

read_token() { tr -d ' \t\n\r' < "$TOKENFILE" 2>/dev/null; }
is_token() { printf '%s' "$1" | grep -qE '^[0-9a-f]{32}$'; }

mkdir -p "$STATE" 2>/dev/null || true
TOKEN="$(read_token)"
if ! is_token "$TOKEN"; then
  NEW="$(openssl rand -hex 16 2>/dev/null || (head -c16 /dev/urandom | xxd -p | tr -d '\n') 2>/dev/null)"
  if is_token "$NEW"; then
    # Atomic create so a racing daemon boot / sibling session converges on one token: if the file
    # appeared first, keep theirs. `set -C` (noclobber) makes `>` fail when the file already exists.
    if ( set -C; printf '%s' "$NEW" > "$TOKENFILE" ) 2>/dev/null; then
      chmod 600 "$TOKENFILE" 2>/dev/null || true
      TOKEN="$NEW"
    else
      TOKEN="$(read_token)"
    fi
  fi
fi

# Worker recursion guard (layer 1 over HTTP): a Medley worker inherits the plugin, so its own CC
# session connects here too. It sends X-Medley-Worker so the daemon binds the no-op stub, never the
# orchestrator (deniedTools is the independent layer 2). The worker's daemon is already up — no nudge.
if [ "${MEDLEY_WORKER:-}" = "1" ]; then
  printf '{"Authorization":"Bearer %s","X-Medley-Worker":"1"%s}\n' "$TOKEN" "$PIN_HDR"
  exit 0
fi

# Cold-start bridge: MCP connect races the SessionStart pre-warm (they run concurrently), so the daemon
# may not be listening yet — and on a FRESH same-session install (marketplace add → install →
# /reload-plugins, no restart) the SessionStart hook never fired, so the engine binary was never
# downloaded at all. When nothing answers on the port, kick a fully-detached bootstrap that (1) ensures
# the binary is present — ensure-engine.sh is idempotent: a fast no-op when already cached, an ~80MB
# download when missing; this is what closes the fresh-install hole the pure resolver could not — then
# (2) starts the daemon. The whole thing is backgrounded so the helper still prints its JSON within CC's
# 10s budget and never blocks on a download. ensure-engine.sh has its own single-flight lock, so a
# concurrent SessionStart download is safe. CLAUDE_PLUGIN_DATA is threaded in as $1 (see above);
# CLAUDE_PLUGIN_ROOT is inherited from the helper's env. `service start` no-ops fast when a healthy
# daemon already answers.
if ! curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
  DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  (
    CLAUDE_PLUGIN_DATA="${DATADIR}" "$DIR/ensure-engine.sh" >/dev/null 2>&1 || true
    ENGINE="$("$DIR/resolve-engine.sh" 2>/dev/null || true)"
    [ -n "$ENGINE" ] || exit 0
    case "$ENGINE" in
      *.cjs|*.js|*.mjs) node "$ENGINE" service start >/dev/null 2>&1 || true ;;
      *)                "$ENGINE" service start >/dev/null 2>&1 || true ;;
    esac
  ) &
fi

# An AWAY TURN declares itself, so the daemon serves the away flavour of the orchestrator: no
# `mission_wait` (it would hold the mission's composer slot for minutes with nothing able to interrupt
# it) and supervision guidance that says "the engine wakes you" instead of "arm a background watcher" —
# which a headless turn cannot do, and which it kept visibly trying to.
#
# This replaces an in-proc `type:'sdk'` server the engine used to inject for the same purpose. That
# server was keyed `plugin_medley_medley`, the same name THIS entry already has, so two servers claimed
# one tool prefix — and being served over the SDK↔CLI control protocol it vanished mid-turn, taking
# every tool call with it. Over HTTP there is one server and a control-protocol hiccup cannot take it.
#
# MEDLEY_RESUME is set by the engine on the turn it resumes (host-session-resume's RESUME_ENV) and
# reaches this helper the way it reaches the plugin's hooks — the spawned Claude Code process hands its
# environment to its subprocesses. Absent → nothing is added, so every other client is untouched.
AWAY_HDR=""
if [ "${MEDLEY_RESUME:-}" = "1" ]; then AWAY_HDR=',"X-Medley-Away":"1"'; fi

printf '{"Authorization":"Bearer %s"%s%s%s}\n' "$TOKEN" "$PIN_HDR" "$TERM_HDR" "$AWAY_HDR"
