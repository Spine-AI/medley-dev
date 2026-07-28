#!/usr/bin/env bash
# Dev loop: (re)install this checkout's plugin/ into Codex CLI.
#
# The Codex analogue of `claude --plugin-dir ./plugin` does not exist — Codex only loads plugins it
# has COPIED into ~/.codex/plugins/cache from a configured marketplace. So iterating means: bump a
# cachebuster, reinstall, start a NEW thread. This script does the first two.
#
# Usage:
#   scripts/codex-dev-install.sh                      # use the downloaded engine (~/.medley/engine-path)
#   scripts/codex-dev-install.sh /path/to/medley-engine.cjs   # pin a local engine build
#   scripts/codex-dev-install.sh --clear-engine       # drop the local-build pin
#
# A local engine build is pinned via ~/.medley/engine-override rather than $MEDLEY_ENGINE, because a
# Codex plugin MCP server inherits NO environment from the session (see plugin/scripts/medley-mcp.sh).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN="$REPO/plugin"
MARKETPLACE="medley-dev"
VALIDATOR="$HOME/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py"

case "${1:-}" in
  --clear-engine)
    rm -f "$HOME/.medley/engine-override"
    echo "✓ cleared ~/.medley/engine-override (back to the downloaded engine)"
    ;;
  "") ;;
  *)
    [ -f "$1" ] || { echo "no such engine build: $1" >&2; exit 1; }
    mkdir -p "$HOME/.medley"
    printf '%s\n' "$(cd "$(dirname "$1")" && pwd)/$(basename "$1")" > "$HOME/.medley/engine-override"
    echo "✓ pinned engine → $(cat "$HOME/.medley/engine-override")"
    ;;
esac

# 1. Install the fixed-path MCP launcher. The SessionStart hook does this too, but a Codex session
#    starts plugin MCP servers without ever guaranteeing the hook ran (and the hook trust gate can
#    block it), so the dev loop installs it directly and does not depend on that.
mkdir -p "$HOME/.medley/bin"
install -m 0755 "$PLUGIN/scripts/medley-mcp.sh" "$HOME/.medley/bin/medley-mcp"
echo "✓ installed ~/.medley/bin/medley-mcp"

# 2. Validate both manifests before anything touches Codex's config.
if [ ! -f "$VALIDATOR" ]; then
  echo "! validate_plugin.py not found — skipping Codex manifest validation" >&2
elif command -v uv >/dev/null 2>&1; then
  # It imports pyyaml, which the system python3 does not have.
  uv run --quiet --with pyyaml python "$VALIDATOR" "$PLUGIN" >/dev/null && echo "✓ codex validate_plugin.py"
else
  python3 "$VALIDATOR" "$PLUGIN" >/dev/null && echo "✓ codex validate_plugin.py"
fi
if command -v claude >/dev/null 2>&1; then
  claude plugin validate "$PLUGIN" --strict >/dev/null && echo "✓ claude plugin validate --strict"
fi

# 3. Register the repo-local marketplace. Not discovered implicitly (only ~/.agents/plugins is), and
#    re-adding an existing one is a no-op refresh.
codex plugin marketplace add "$REPO" >/dev/null 2>&1 || true
echo "✓ marketplace $MARKETPLACE → $REPO"

# 4. Install. NO cachebuster — deliberately, against the plugin-creator skill's advice.
#
#    That advice assumes the cache would otherwise serve a stale copy. It does not for a LOCAL
#    marketplace: `codex plugin add` at an unchanged version re-copies from the source path anyway
#    (verified with a probe file). So the suffix buys nothing here — and it actively breaks running
#    sessions. Codex names the cache dir after the version the SOURCE manifest declares, and
#    reconciles a disagreement by re-materializing and PRUNING the old dir. Cachebusting installs to
#    `…/0.8.6-dev.0+codex.<ts>/` while the committed source still says `0.8.6-dev.0`, so the next
#    session start renames the directory out from under any session already bound to it. Every
#    absolute path that session captured then dangles: `session-start.sh` fails to exec (127), and
#    `edit-conflict-gate.py` makes python exit 2 — which is precisely Claude's (and Codex's)
#    "PreToolUse denied" signal, so a missing file silently becomes a blocked tool call.
#
#    Keeping the source version and the cache dir in agreement is what makes reinstalls safe.
codex plugin add "medley@$MARKETPLACE"

echo
echo "Pin:    $(tr -d ' \t\n\r' < "$PLUGIN/engine/version")  (plugin/engine/version)"
echo "Engine: $(cat "$HOME/.medley/engine-override" 2>/dev/null || cat "$HOME/.medley/engine-path" 2>/dev/null || echo 'not resolved yet')"
if [ -f "$HOME/.medley/engine-override" ]; then
  echo "        ^ local build pinned via ~/.medley/engine-override — clear it with --clear-engine"
else
  echo "        ^ the next Codex thread's SessionStart hook fetches the pin and rolls the daemon"
fi
echo
echo "Now start a NEW Codex thread — plugins and their MCP tools are bound at thread start — then"
echo "type \$medley:mission (Codex has no /mission: plugins can't add slash commands, skills use \$)."
echo "Nothing you edit under plugin/ reaches Codex until this script runs again."
