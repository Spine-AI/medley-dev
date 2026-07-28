#!/usr/bin/env bash
# Fixed-path launcher for the Medley MCP proxy, for hosts that hand an MCP server NO plugin
# environment.
#
# Codex 0.145 is such a host. A plugin-contributed MCP server process inherits a bare env —
# measured, exhaustively: HOME LANG LOGNAME PATH PWD SHELL SHLVL TERM TMPDIR USER — and the manifest
# does NOT interpolate ${PLUGIN_ROOT}. `PLUGIN_ROOT`/`CLAUDE_PLUGIN_ROOT`/`PLUGIN_DATA`/
# `CLAUDE_PLUGIN_DATA` are injected by Codex's *hook* command runner only. So from a Codex
# `mcpServers` command string there is no way to name the plugin's own directory, and
# run-engine.sh (which self-locates via BASH_SOURCE) can never be reached.
#
# Same problem, same answer as the statusline: keep a STABLE copy at a fixed path outside the
# versioned plugin cache — session-start.sh installs this file as ~/.medley/bin/medley-mcp exactly
# as it installs ~/.medley/statusline.sh — and have the manifest address that path. It also survives
# a plugin-cache prune, which a versioned path does not.
#
# Resolution deliberately differs from resolve-engine.sh: with no plugin env there is no
# ${CLAUDE_PLUGIN_DATA}/bin/medley-engine-<pin> to check, so the ~/.medley/engine-path cache
# (written monotonically by ensure-engine.sh) is the primary source. That means this launcher tracks
# the newest installed engine rather than the manifest pin — acceptable for the mission proxy, which
# is version-tolerant, and the reason the gateway (`mcp --gateway`, pin-strict by design) is NOT
# routed through here.
#
# stdout is the JSON-RPC channel — every diagnostic MUST go to stderr.
set -u

ENGINE=""
for candidate in \
  "${MEDLEY_ENGINE:-}" \
  "$(cat "${HOME:-/nonexistent}/.medley/engine-override" 2>/dev/null || true)" \
  "$(cat "${HOME:-/nonexistent}/.medley/engine-path" 2>/dev/null || true)"; do
  if [ -n "$candidate" ] && [ -f "$candidate" ]; then
    ENGINE="$candidate"
    break
  fi
done

if [ -z "$ENGINE" ]; then
  echo "medley: no engine binary found. Expected a path in ~/.medley/engine-path, which the" >&2
  echo "        plugin's SessionStart hook writes once the engine has been downloaded. Start a" >&2
  echo "        new session and try again; if it persists, the download failed (check network)." >&2
  exit 1
fi

case "$ENGINE" in
  *.cjs|*.js|*.mjs) exec node "$ENGINE" "$@" ;;  # dev build → run via node
  *)                exec "$ENGINE" "$@" ;;       # self-contained binary
esac
