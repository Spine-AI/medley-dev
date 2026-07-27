#!/bin/bash
# Statusline command: e.g. `medley ▸ Fix login · RUNNING · 4/9 · 2 workers · ⚡ NEEDS YOU (1)`
# (empty when no mission is active or when gated off). Wired in
# settings.json (see the plugin README):
#   "statusLine": { "type": "command", "command": "/abs/path/to/plugin/scripts/statusline.sh" }
# Runs in a settings.json context that lacks ${CLAUDE_PLUGIN_DATA}; resolve-engine.sh falls back
# to the ~/.medley/engine-path cache that session-start.sh writes. Never installs (stays instant).
# Claude Code re-invokes this on a ~300ms throttle, and each engine call cold-starts the whole binary
# + opens SQLite — so the slow path below serves a short-TTL per-repo cache (a file read) between
# engine spawns. See the cache block near the bottom.

# Update-state fast path (engine-free): while an engine download or a version roll is in flight,
# surface it straight from $HOME-reachable state — ensure-engine.sh's update.json download breadcrumb
# and the engine daemon's .rolling marker. Runs BEFORE resolving the engine because during a first
# install / binary swap the engine may not be runnable yet (the one moment we most want to show it).
STATE_DIR="${MEDLEY_DATA_DIR:-$HOME/.medley/state}"
if [ -f "$STATE_DIR/update.json" ] || [ -f "$STATE_DIR/.rolling" ]; then
  ind=$(STATE_DIR="$STATE_DIR" python3 - <<'PY' 2>/dev/null
import json, os, time
sd = os.environ.get("STATE_DIR", "")
now = time.time() * 1000
line = ""
# download breadcrumb — 30-min crash-net (ensure-engine.sh removes it when the download settles)
try:
    with open(os.path.join(sd, "update.json")) as f:
        u = json.load(f)
    if u.get("state") == "downloading" and now - float(u.get("since", 0)) < 1_800_000:
        v = u.get("version", "")
        line = "medley ▸ ⟳ downloading engine" + ((" v" + v) if v else "") + "…"
    elif u.get("state") == "pending":
        # A newer engine is installed but the roll is DEFERRED until the daemon goes idle (the engine
        # writes this + clears it on roll). No freshness bound: a deferred upgrade can legitimately
        # wait hours (e.g. a worker sleeping on a wakeup) before it applies.
        v = u.get("version", "")
        line = "medley ▸ ⟳ update" + ((" v" + v) if v else "") + " pending"
except Exception:
    pass
# version roll — 60s freshness (matches roll-marker.ts FRESH_MS)
if not line:
    try:
        with open(os.path.join(sd, ".rolling")) as f:
            ts = float(f.read().strip())
        if now - ts < 60_000:
            line = "medley ▸ ⟳ updating engine…"
    except Exception:
        pass
if line:
    print(line)
PY
)
  if [ -n "$ind" ]; then printf '%s' "$ind"; exit 0; fi
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Resolve the engine. In the plugin cache the sibling resolve-engine.sh handles it (incl. the dev
# MEDLEY_ENGINE override); when this file is the STABLE installed copy (~/.medley/statusline.sh —
# outside the plugin cache, see session-start.sh) there is no sibling, so fall back to the engine-path
# cache directly. Either way the statusline survives a plugin-cache version bump / prune.
if [ -x "$DIR/resolve-engine.sh" ]; then
  ENGINE="$("$DIR/resolve-engine.sh" 2>/dev/null || true)"
else
  ENGINE="$(cat "${HOME}/.medley/engine-path" 2>/dev/null || true)"
fi
[ -n "$ENGINE" ] && [ -f "$ENGINE" ] || exit 0
input=$(cat)
# The repo (project) this statusline is for — drives BOTH the per-repo cache key and the engine's cwd
# (the engine renders the mission for process.cwd()). Pure-sed extraction, so the hot path spawns no
# python interpreter; mirrors session-start.sh's JSON-field parsing. One var, defaulted to $PWD, so an
# empty project_dir can't bucket unrelated sessions into a shared cache entry.
repo=$(printf '%s' "$input" | sed -n 's/.*"project_dir"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)
[ -n "$repo" ] || repo=$(printf '%s' "$input" | sed -n 's/.*"cwd"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)
[ -n "$repo" ] || repo="$PWD"

# Short-TTL per-repo cache. Within TTL seconds serve the cached line (a file read, no engine); only a
# miss cold-starts the engine — so engine spawns are bounded to ≤ once / TTL / repo regardless of how
# fast Claude Code ticks. Staleness ≤ TTL (default 2s — imperceptible). MEDLEY_STATUSLINE_TTL=0 disables
# it (tests / opt-out). The cache file stores the repo path on line 1 and the rendered line as the
# remainder; a hit is served ONLY when line 1 matches this repo, so a cksum key collision can never
# leak one repo's mission into another. Freshness is pure bash + stat/date (BSD/macOS — Medley is mac-only).
TTL="${MEDLEY_STATUSLINE_TTL:-2}"
case "$TTL" in ''|*[!0-9]*) TTL=2 ;; esac   # non-integer override → safe default (avoids arithmetic errors)
CACHE_DIR="${MEDLEY_DATA_DIR:-$HOME/.medley/state}/sl-cache"
key=$(printf '%s' "$repo" | cksum | cut -d' ' -f1)
cache="$CACHE_DIR/$key"
if [ "$TTL" -gt 0 ] && [ -f "$cache" ]; then
  mtime=$(stat -f %m "$cache" 2>/dev/null || echo 0)
  now=$(date +%s)
  if [ $(( now - mtime )) -lt "$TTL" ]; then
    IFS= read -r cached_repo < "$cache"   # line 1 = the repo this cache entry is for (bash builtin, no exec)
    if [ "$cached_repo" = "$repo" ]; then tail -n +2 "$cache"; exit 0; fi   # HIT: verified same repo
  fi
fi

# Miss (stale / different repo / cache disabled): run the engine, capture, refresh the cache atomically
# (tmp+mv), print. No `exec` — we need the output to store it. Empty output is cached too, so an idle /
# gated-off repo doesn't re-spawn the engine every tick.
runner=""; case "$ENGINE" in *.cjs|*.js|*.mjs) runner="node" ;; esac   # case OUTSIDE $() — nesting it trips the parser
if [ -n "$runner" ]; then
  out=$(cd "$repo" 2>/dev/null || :; "$runner" "$ENGINE" status --statusline 2>/dev/null)
else
  out=$(cd "$repo" 2>/dev/null || :; "$ENGINE" status --statusline 2>/dev/null)
fi
if [ "$TTL" -gt 0 ]; then
  mkdir -p "$CACHE_DIR" 2>/dev/null || true
  tmp="$cache.tmp.$$"
  printf '%s\n%s' "$repo" "$out" > "$tmp" 2>/dev/null && mv -f "$tmp" "$cache" 2>/dev/null || rm -f "$tmp" 2>/dev/null || true
fi
printf '%s' "$out"
