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

# WHICH COPY AM I? Asked structurally — by location — and NOT inferred from "did I find a pin". Those
# are different questions and conflating them is a live hazard: a plugin dir whose engine/version is
# momentarily unreadable (a --plugin-dir checkout mid-edit, a partially materialized cache) would
# otherwise fall through to the Codex breadcrumbs and launch whatever engine THAT cache holds — an
# older binary ignores `--gateway` and serves the ORCHESTRATOR under the gateway's name, the exact
# failure this script exists to prevent. Baseline behavior for an unpinned plugin dir is to REFUSE, and
# it must stay that way. Both sides are resolved through `cd && pwd` so a symlinked $HOME still matches.
FIXED_DIR="$(cd "$MEDLEY_HOME/bin" 2>/dev/null && pwd || true)"
IS_FIXED_PATH=""
if [ -n "$FIXED_DIR" ] && [ "$DIR" = "$FIXED_DIR" ]; then IS_FIXED_PATH=1; fi
# ── FIXED-PATH FALLBACKS ────────────────────────────────────────────────────────────────────────────
# Everything in this block is reached ONLY by the fixed-path copy at ~/.medley/bin/medley-gateway, the
# one a host with no plugin env (Codex) launches. Claude Code always launches the copy inside the plugin
# dir, so on that path this block is DEAD CODE and resolution stays byte-identical to what shipped
# before it existed — verified by A/B against the pre-change file across every scenario CC can produce.
# One gate, checked once, covering all three fallbacks, so no future edit can leak one of them onto the
# Claude path by accident.
#
#   codex-engine-pin  — the PIN VALUE, deliberately NOT a plugin root. A root would point into the
#     VERSIONED Codex cache (…/cache/<market>/medley/<ver>/), which Codex renames and prunes whenever
#     the source manifest's version changes — precisely the dangling-path failure that made
#     session-start.sh exit 127 and turned a missing edit-conflict-gate.py into a "PreToolUse denied".
#     A pin is just a string: it can go STALE but never dangle, and stale fails closed (the binary for
#     that version isn't there, so we refuse below). SessionStart rewrites it at thread start, before
#     any tool call can reach the gateway.
#   codex-plugin-data — the data dir holding the downloaded binaries. Safe to store as a path: unlike
#     the plugin cache, ~/.codex/plugins/data/<plugin>-<marketplace> is not versioned.
#   engine-override   — the local-build pin `codex-dev-install.sh` writes, since $MEDLEY_ENGINE cannot
#     be passed to an MCP server on this host. Kept off the Claude path because a stale override would
#     hand CC's gateway an older binary, and an older binary ignores `--gateway` and serves the
#     ORCHESTRATOR under the gateway's name — the exact failure the pin-strict rule exists to prevent.
#     ($MEDLEY_ENGINE stays unconditional below: it was already honored on both paths beforehand.)
OVERRIDE=""
if [ -n "$IS_FIXED_PATH" ]; then
  # `[ -f ]` before every read: with `< missing_file` the failure is reported by the SHELL, not by the
  # command, so `2>/dev/null` on `tr` does NOT suppress it and a "No such file or directory" line leaks
  # onto stderr — noise in a channel the host logs, on the ordinary first-run path.
  if [ -f "$MEDLEY_HOME/codex-engine-pin" ]; then
    VERSION="$(tr -d ' \t\n\r' < "$MEDLEY_HOME/codex-engine-pin" 2>/dev/null)"
  else
    VERSION=""
  fi
  if [ -z "$DATADIR" ] && [ -f "$MEDLEY_HOME/codex-plugin-data" ]; then
    DATADIR="$(tr -d ' \t\n\r' < "$MEDLEY_HOME/codex-plugin-data" 2>/dev/null)"
  fi
  if [ -f "$MEDLEY_HOME/engine-override" ]; then
    OVERRIDE="$(tr -d ' \t\n\r' < "$MEDLEY_HOME/engine-override" 2>/dev/null)"
  fi
fi

ENGINE=""
if [ -n "${MEDLEY_ENGINE:-}" ] && [ -f "${MEDLEY_ENGINE}" ]; then
  ENGINE="${MEDLEY_ENGINE}" # explicit dev override (a local .cjs or binary)
elif [ -n "$OVERRIDE" ] && [ -f "$OVERRIDE" ]; then
  ENGINE="$OVERRIDE"        # ~/.medley/engine-override — fixed-path only (Codex dev loop)
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
