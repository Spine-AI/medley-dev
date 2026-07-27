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

printf '{"Authorization":"Bearer %s"%s}\n' "$TOKEN" "$PIN_HDR"
