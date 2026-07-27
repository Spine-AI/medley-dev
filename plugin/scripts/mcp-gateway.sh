#!/usr/bin/env bash
# stdio launcher for the `medley_gateway` MCP server (.mcp.json, type:stdio) — the centralized MCP
# gateway (your connected apps) for a TOP-LEVEL Claude Code session.
#
# Why stdio when `medley` itself is direct-http: MCP hosts DEDUP servers by origin + PATHNAME (a
# query string is normalized away — verified against Claude Code), and both servers live on the same
# daemon. Two http entries on `/mcp` collapse into one ("MCP server medley_gateway skipped — same
# command/URL as server provided by plugin medley"), which silently dropped the gateway. Giving the
# gateway its own http PATH fixed dedup but introduced a version floor: a session whose daemon is
# still on an older engine 404s that path, so the gateway broke for exactly one session after every
# upgrade. A stdio entry is deduped by its COMMAND, so it can't collide — and it talks to `/mcp` with
# the `X-Medley-Worker` selector, the path+header EVERY shipped engine already serves. No daemon
# version floor, no upgrade window.
#
# The floor moves to THIS side instead, and that's the part to get right: the engine binary we exec
# must be the one the plugin is PINNED to, because only a pinned-era binary understands
# `mcp --gateway`. An older binary silently ignores the unknown flag and serves the ORCHESTRATOR —
# which would surface mission tools under the gateway's name (a confusing duplicate), far worse than
# a clean failure. So resolution here is STRICT: the pinned binary or nothing. We deliberately do NOT
# fall back to `~/.medley/engine-path` (the resolve-engine.sh cache), which may point at an older
# binary. That's the one behavioral difference from run-engine.sh.
#
# stdout is the JSON-RPC channel — every diagnostic MUST go to stderr.
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# CC interpolates ${CLAUDE_PLUGIN_DATA} into a stdio server's args, so .mcp.json passes it as $1 (the
# same contract mcp-headers.sh uses). Measured: it also lands in the server's env — but $1 stays the
# primary, since arg interpolation is the documented behavior and the env var is not guaranteed across
# install modes. ensure-engine.sh hard-requires this value.
DATADIR="${1:-${CLAUDE_PLUGIN_DATA:-}}"

# The pin is read from OUR OWN location first, not from $CLAUDE_PLUGIN_ROOT. Measured: CC DOES set
# that env var for a stdio MCP server (verified via --plugin-dir), so reading it would work — but the
# whole failure mode this file exists to end was betting the gateway on an environment detail that
# holds in one mode and not another. An empty root would read the pin as "" and make this script
# refuse to start on EVERY session, silently. This file always lives at <plugin root>/scripts/, so
# `..` is the root by construction, in every mode. Env var kept as a fallback.
PLUGIN_ROOT="$(cd "$DIR/.." && pwd)"
VERSION=""
for root in "$PLUGIN_ROOT" "${CLAUDE_PLUGIN_ROOT:-}"; do
  if [ -n "$root" ] && [ -f "$root/engine/version" ]; then
    VERSION="$(tr -d ' \t\n\r' < "$root/engine/version" 2>/dev/null)"
    if [ -n "$VERSION" ]; then break; fi
  fi
done

ENGINE=""
if [ -n "${MEDLEY_ENGINE:-}" ] && [ -f "${MEDLEY_ENGINE}" ]; then
  ENGINE="${MEDLEY_ENGINE}" # explicit dev override (a local .cjs or binary)
elif [ -n "$VERSION" ] && [ -n "$DATADIR" ]; then
  ENGINE="${DATADIR}/bin/medley-engine-${VERSION}"
  if [ ! -f "$ENGINE" ]; then
    # Not downloaded yet (a fresh install, or this connect beat the SessionStart bootstrap). Fetch it
    # now — ensure-engine.sh is idempotent and single-flight-locked, so a concurrent SessionStart
    # download is safe and we simply wait for it. stdout redirected: it must not pollute JSON-RPC.
    # BOTH vars are passed explicitly: ensure-engine.sh resolves the pin from $CLAUDE_PLUGIN_ROOT and
    # `exit 0`s when it finds no version file — inheriting an unset root would turn this rescue into a
    # silent no-op, then we'd fail below with "download pending" and never actually download.
    CLAUDE_PLUGIN_ROOT="$PLUGIN_ROOT" CLAUDE_PLUGIN_DATA="$DATADIR" "$DIR/ensure-engine.sh" >&2 2>&1 || true
  fi
fi

if [ -z "$ENGINE" ] || [ ! -f "$ENGINE" ]; then
  echo "medley: the gateway needs engine v${VERSION:-?}, which isn't available yet (download pending or" >&2
  echo "        offline). Your mission tools are unaffected; connected apps return on the next session." >&2
  exit 1
fi

case "$ENGINE" in
  *.cjs|*.js|*.mjs) exec node "$ENGINE" mcp --gateway ;; # dev build → run via node
  *)                exec "$ENGINE" mcp --gateway ;;      # self-contained binary
esac
