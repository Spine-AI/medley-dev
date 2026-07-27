#!/usr/bin/env bash
# Resolve the Medley engine EXECUTABLE. Prints the absolute path and exits 0 if found; else exits 1
# with no output. Pure — no side effects (see ensure-engine.sh for the downloader).
#
# The engine ships as a self-contained, notarized binary (no Node required). Resolution order:
#   1. $MEDLEY_ENGINE                                   — explicit dev override (a local .cjs or binary)
#   2. ${CLAUDE_PLUGIN_DATA}/bin/medley-engine-<ver>    — the downloaded binary for the pinned version
#   3. path cached in ~/.medley/engine-path             — for contexts without the plugin env
#      (e.g. the statusline command, wired via settings.json where ${CLAUDE_PLUGIN_DATA} is unset;
#       session-start.sh writes this cache each session).
version=""
[ -f "${CLAUDE_PLUGIN_ROOT:-/nonexistent}/engine/version" ] && version="$(tr -d ' \t\n\r' < "${CLAUDE_PLUGIN_ROOT}/engine/version" 2>/dev/null)"
installed="${CLAUDE_PLUGIN_DATA:-/nonexistent}/bin/medley-engine-${version}"
cached=""
[ -f "${HOME:-/nonexistent}/.medley/engine-path" ] && cached="$(cat "${HOME}/.medley/engine-path" 2>/dev/null)"
for candidate in \
  "${MEDLEY_ENGINE:-}" \
  "$installed" \
  "$cached"; do
  if [ -n "$candidate" ] && [ -f "$candidate" ]; then
    printf '%s\n' "$candidate"
    exit 0
  fi
done
exit 1
