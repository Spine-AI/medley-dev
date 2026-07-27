#!/usr/bin/env bash
# Resolve (downloading on demand) the Medley engine and exec it with the given args.
# Used by .mcp.json to launch the MCP proxy (`run-engine.sh mcp`); also usable ad hoc.
# The engine is normally a self-contained binary (run directly); a dev .cjs build is run via node.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ENGINE="$("$DIR/resolve-engine.sh" 2>/dev/null || true)"
if [ -z "$ENGINE" ]; then
  # Not present yet (e.g. very first session before the SessionStart hook finished) — download now.
  "$DIR/ensure-engine.sh" || true
  ENGINE="$("$DIR/resolve-engine.sh" 2>/dev/null || true)"
fi
if [ -z "$ENGINE" ]; then
  echo "medley: engine not found and could not be downloaded. Check your network connection and" >&2
  echo "        restart Claude Code (the engine is fetched automatically from GitHub Releases)." >&2
  exit 1
fi
case "$ENGINE" in
  *.cjs|*.js|*.mjs) exec node "$ENGINE" "$@" ;;   # dev build → run via node
  *)                exec "$ENGINE" "$@" ;;         # self-contained binary
esac
