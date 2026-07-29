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
MEDLEY_HOME="${HOME:-/nonexistent}/.medley"

# CC interpolates ${CLAUDE_PLUGIN_DATA} into a stdio server's args, so .mcp.json passes it as $1 (the
# same contract mcp-headers.sh uses). Measured: it also lands in the server's env — but $1 stays the
# primary, since arg interpolation is the documented behavior and the env var is not guaranteed across
# install modes. ensure-engine.sh hard-requires this value.
#
# ON A HOST WITH NO PLUGIN ENV (Codex) neither is available: the manifest does not interpolate
# ${VAR} and the MCP server process inherits a bare env (measured — see medley-mcp.sh). This same
# script is therefore ALSO installed at ~/.medley/bin/medley-gateway by session-start.sh, and the
# hook — which DOES get the plugin env — leaves both halves of the resolution at fixed paths for us
# to read here. That is the whole reason the gateway can keep its pin-strict rule on that host
# instead of being routed through medley-mcp.sh, which only tracks the newest installed engine.
DATADIR="${1:-${CLAUDE_PLUGIN_DATA:-}}"
if [ -z "$DATADIR" ]; then
  DATADIR="$(cat "$MEDLEY_HOME/codex-plugin-data" 2>/dev/null || true)"
fi

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
# Fixed-path fallback for the no-plugin-env host: when this file runs from ~/.medley/bin, `..` is
# ~/.medley and holds no engine/version, so we land here. The breadcrumb carries the PIN VALUE and
# deliberately NOT a plugin root: a root would point into the VERSIONED Codex plugin cache
# (…/cache/<market>/medley/<ver>/), which Codex renames and prunes whenever the source manifest's
# version changes — precisely the dangling-path failure that made session-start.sh exit 127 and
# turned a missing edit-conflict-gate.py into a "PreToolUse denied". A pin is just a string: it can
# go STALE but never dangle, and stale fails closed (the binary for that version isn't there, so we
# refuse below) rather than executing something unexpected. SessionStart rewrites it at thread start,
# before any tool call can reach the gateway.
if [ -z "$VERSION" ]; then
  VERSION="$(tr -d ' \t\n\r' < "$MEDLEY_HOME/codex-engine-pin" 2>/dev/null || true)"
fi

# The local-build pin used by scripts/codex-dev-install.sh. Honored here for the same reason
# medley-mcp.sh honors it: on Codex there is no way to pass $MEDLEY_ENGINE to an MCP server, so a
# developer testing a local engine would otherwise get a local-build mission proxy and a
# downloaded-binary gateway in the same session. Both entries are explicit developer overrides and
# rank above the pin — a dev who points these at an old build owns that choice.
OVERRIDE="$(cat "$MEDLEY_HOME/engine-override" 2>/dev/null || true)"

ENGINE=""
if [ -n "${MEDLEY_ENGINE:-}" ] && [ -f "${MEDLEY_ENGINE}" ]; then
  ENGINE="${MEDLEY_ENGINE}" # explicit dev override (a local .cjs or binary)
elif [ -n "$OVERRIDE" ] && [ -f "$OVERRIDE" ]; then
  ENGINE="$OVERRIDE"        # ~/.medley/engine-override — the Codex dev loop's local-build pin
elif [ -n "$VERSION" ] && [ -n "$DATADIR" ]; then
  ENGINE="${DATADIR}/bin/medley-engine-${VERSION}"
  # The rescue below needs ensure-engine.sh NEXT TO US, which is true only in the plugin dir. From
  # ~/.medley/bin there is nothing to call (and nothing to call it with — no plugin root to read a
  # pin from), so we skip straight to the refusal, which the Codex path recovers from on the next
  # SessionStart. Guarded rather than unconditional so a missing file can't print a `command not
  # found` line into a channel a host may be parsing.
  if [ ! -f "$ENGINE" ] && [ -x "$DIR/ensure-engine.sh" ]; then
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
  echo "medley: the gateway needs engine v${VERSION:-?}, which isn't available yet (download pending," >&2
  echo "        offline, or this host's SessionStart hook has not run yet). Your mission tools are" >&2
  echo "        unaffected; connected apps return on the next session." >&2
  exit 1
fi

case "$ENGINE" in
  *.cjs|*.js|*.mjs) exec node "$ENGINE" mcp --gateway ;; # dev build → run via node
  *)                exec "$ENGINE" mcp --gateway ;;      # self-contained binary
esac
