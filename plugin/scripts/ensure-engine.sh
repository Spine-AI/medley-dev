#!/usr/bin/env bash
# SessionStart bootstrap: make sure the pinned Medley engine BINARY for this platform is downloaded
# into the plugin's persistent data dir (${CLAUDE_PLUGIN_DATA}/bin). The engine ships as a
# self-contained, code-signed executable served from the R2 CDN (engine.getmedley.ai) with
# the public GitHub Release as a fallback mirror — so there is no auth, no npm, and no Node
# requirement. Downloads only when the pinned version isn't already cached.
# No-ops for workers and for the dev override. Fails soft (exits 0) so the session always starts.
set -u

# Workers inherit the plugin; they were spawned by a live engine and must never (re)download.
[ "${MEDLEY_WORKER:-}" = "1" ] && exit 0
# Dev override: a local build is in use — nothing to download.
[ -n "${MEDLEY_ENGINE:-}" ] && exit 0
# The data dir is only provided by Claude Code at hook runtime; without it we can't cache.
[ -n "${CLAUDE_PLUGIN_DATA:-}" ] || exit 0

VERSION_FILE="${CLAUDE_PLUGIN_ROOT:-}/engine/version"
[ -f "$VERSION_FILE" ] || exit 0
VERSION="$(tr -d ' \t\n\r' < "$VERSION_FILE")"
[ -n "$VERSION" ] || exit 0

BIN_DIR="${CLAUDE_PLUGIN_DATA}/bin"
BIN_PATH="${BIN_DIR}/medley-engine-${VERSION}"

# Order version strings newest-FIRST on stdout. Handles the dev channel's `X.Y.Z-dev.N` prereleases
# by folding N into a fourth numeric field, matching the engine's cmp() (runtime-version.ts), which
# parseInt's each dot-separated field — so `0.8.6-dev.1` is [0,8,6,1]. An explicit zero-padded key is
# built here rather than leaning on `sort -t.` field keys: for `0.8.6-dev.1` the third field is
# `6-dev` and the fourth is never compared, so two dev builds of one x.y.z tied on EVERY key and fell
# back to whole-line lexicographic order. That stalled the engine-path pointer (dev.1 never advanced
# past dev.0), let a stale session stamp it backward, and made prune treat the oldest build as newest.
# (macOS BSD sort has no -V, hence the hand-built key.) The sort is `-s` (stable) keyed on ONLY the
# first field (the numeric key) — equal keys must preserve input order, because `version_ge` relies on
# a cmp-equal pair (e.g. a stable X.Y.Z and its own X.Y.Z-dev.0, both [X,Y,Z,0]) resolving to `$1`, the
# first line piped in; sorting on the whole line (or a plain `sort -r`) would fall through to a
# whole-line tiebreak instead, where the shorter/stable string always loses regardless of argument order.
vsort_desc() {
  awk '{
    v = $0; sub(/-dev\./, ".", v); n = split(v, p, ".")
    printf "%010d.%010d.%010d.%010d\t%s\n", p[1]+0, p[2]+0, p[3]+0, (n > 3 ? p[4]+0 : 0), $0
  }' | sort -s -r -k1,1 | cut -f2
}

# Portable "is $1 >= $2" for dotted x.y.z[-dev.N] versions.
version_ge() {
  [ "$1" = "$2" ] && return 0
  [ "$(printf '%s\n%s\n' "$1" "$2" | vsort_desc | head -1)" = "$1" ]
}

# Record the engine-path cache the statusline + resolver fall back to — but only ever ADVANCE it. A
# stale older-pin session (a concurrent Claude Code window on a prior plugin cache) must not downgrade
# the cache and point everything at an older engine.
record_engine_path() {
  local cache="${HOME}/.medley/engine-path" cur cur_ver
  mkdir -p "${HOME}/.medley" 2>/dev/null || return 0
  cur="$(cat "$cache" 2>/dev/null || true)"
  cur_ver="${cur##*/medley-engine-}"
  if [ -z "$cur" ] || [ "$cur_ver" = "$cur" ] || version_ge "$VERSION" "$cur_ver"; then
    # Atomic write (tmp + mv): the launchd trampoline (~/.medley/bin/medley-daemon) reads this file to
    # decide which binary to exec, so a torn/partial read would strand the daemon (exit 78). A rename
    # is atomic on the same filesystem, so the trampoline only ever sees the old or the new path whole.
    local tmp="${cache}.tmp.$$"
    if printf '%s\n' "$BIN_PATH" > "$tmp" 2>/dev/null; then
      mv -f "$tmp" "$cache" 2>/dev/null || rm -f "$tmp" 2>/dev/null || true
    fi
  fi
}

# Already cached for this version? Record the path (for the statusline) and we're done.
if [ -x "$BIN_PATH" ]; then
  record_engine_path
  exit 0
fi

# Map platform → release asset name (must match the release workflow's outputs).
os="$(uname -s)"; arch="$(uname -m)"
case "$os" in
  Darwin) os_tag="darwin" ;;
  *) echo "medley: Medley is macOS-only (this is $os)." >&2; exit 0 ;;
esac
case "$arch" in
  arm64|aarch64) arch_tag="arm64" ;;
  x86_64|amd64)
    # Medley ships a macOS arm64 binary only — there is no darwin-x64 asset to download.
    echo "medley: requires an Apple Silicon (arm64) Mac. This shell reports x86_64 — if you're on" >&2
    echo "        Apple Silicon it's running under Rosetta 2; relaunch Claude Code in a native arm64" >&2
    echo "        terminal. (Intel Macs are not supported.)" >&2
    exit 0 ;;
  *) echo "medley: unsupported architecture '$arch'." >&2; exit 0 ;;
esac
asset="medley-engine-${os_tag}-${arch_tag}"
# Primary: the R2 download CDN (branded domain, zero egress). Fallback: the public GitHub Release.
# Both are populated by the engine repo's release workflow.
base_r2="https://engine.getmedley.ai/v${VERSION}"
base_gh="https://github.com/Spine-AI/medley/releases/download/v${VERSION}"

mkdir -p "$BIN_DIR"
# Single-flight download lock. On a cold first session, session-start.sh AND run-engine.sh (via
# .mcp.json) can call this concurrently — without a lock they'd fire two curls at the same path and
# race the `mv`. An atomic `mkdir` lock means exactly ONE process downloads; the others wait for the
# binary to appear (bounded) and reuse it. Fail-soft: a stale lock (downloader died) is reclaimed.
LOCK_DIR="${BIN_DIR}/.downloading-${VERSION}.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  for _ in $(seq 1 180); do   # ~90s: wait out the other process's download instead of double-fetching
    if [ -x "$BIN_PATH" ]; then
      record_engine_path
      exit 0
    fi
    sleep 0.5
  done
  rmdir "$LOCK_DIR" 2>/dev/null || true   # looks stale — reclaim and download ourselves
  mkdir "$LOCK_DIR" 2>/dev/null || true
fi
# Status breadcrumb the statusline reads: mark the download in flight under a $HOME-reachable state
# dir (the statusline context lacks ${CLAUDE_PLUGIN_DATA}, so it can't watch the download lock). The
# EXIT trap clears it whether the download succeeds, fails, or the checksum is rejected.
STATE_DIR="${MEDLEY_DATA_DIR:-$HOME/.medley/state}"
UPDATE_FILE="${STATE_DIR}/update.json"
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true; rm -f "$UPDATE_FILE" 2>/dev/null || true' EXIT

echo "medley: downloading engine ${VERSION} (${asset})…" >&2
mkdir -p "$STATE_DIR" 2>/dev/null || true
printf '{"state":"downloading","version":"%s","since":%s}\n' "$VERSION" "$(( $(date +%s) * 1000 ))" > "$UPDATE_FILE" 2>/dev/null || true
tmp="$(mktemp)"
sums="$(mktemp)"
cleanup() { rm -f "$tmp" "$sums"; }

# Try R2 first, then the GitHub Release. Remember which origin served the binary so the checksum
# file is fetched from the same place.
base=""
for b in "$base_r2" "$base_gh"; do
  if curl -fsSL "${b}/${asset}" -o "$tmp"; then base="$b"; break; fi
done
if [ -z "$base" ]; then
  cleanup
  echo "medley: engine download failed (network?) — will retry next session." >&2
  exit 0
fi

# Verify the checksum when SHA256SUMS is published (best-effort — skip if unavailable).
if curl -fsSL "${base}/SHA256SUMS" -o "$sums" 2>/dev/null; then
  expected="$(grep -E "  ${asset}\$" "$sums" 2>/dev/null | awk '{print $1}' | head -1)"
  if [ -n "$expected" ]; then
    if command -v shasum >/dev/null 2>&1; then
      actual="$(shasum -a 256 "$tmp" | awk '{print $1}')"
    else
      actual="$(sha256sum "$tmp" | awk '{print $1}')"
    fi
    if [ "$expected" != "$actual" ]; then
      echo "medley: checksum mismatch for ${asset} — refusing to install." >&2
      cleanup
      exit 0
    fi
  fi
fi

chmod +x "$tmp"
mv "$tmp" "$BIN_PATH"
rm -f "$sums"
# Was this an UPGRADE (a different version already cached) vs a first install? Note it before pruning
# so we can tell the user what happens next.
had_old=""
if [ -n "$(find "$BIN_DIR" -maxdepth 1 -type f -name 'medley-engine-*' ! -name "medley-engine-${VERSION}" -print -quit 2>/dev/null)" ]; then
  had_old="1"
fi
# Prune superseded binaries — each is ~80MB, so old ones (from prior /plugin updates) pile up. KEEP
# THE TWO NEWEST and prune older: the launchd plist hard-codes a versioned binary path, so deleting
# the one it still points at — in the window before the daemon repoints the plist — would strand the
# shared daemon; keeping the current pin + its predecessor closes that window. Only the NEWEST-pin
# session prunes, so a stale older-pin session neither deletes a newer binary nor thrashes
# re-downloading its own. Ordering is vsort_desc (dev-prerelease aware; see its header). Fail-soft.
newest="$(find "$BIN_DIR" -maxdepth 1 -type f -name 'medley-engine-*' 2>/dev/null \
  | sed 's#.*/medley-engine-##' | vsort_desc | head -1)"
if [ "$newest" = "$VERSION" ]; then
  find "$BIN_DIR" -maxdepth 1 -type f -name 'medley-engine-*' 2>/dev/null \
    | sed 's#.*/medley-engine-##' \
    | vsort_desc \
    | tail -n +3 \
    | while IFS= read -r v; do rm -f "$BIN_DIR/medley-engine-$v" 2>/dev/null || true; done
fi
record_engine_path
if [ -n "$had_old" ]; then
  # An upgrade: the shared daemon will roll to ${VERSION} when it next starts (the SessionStart
  # pre-warm triggers it). Sessions reconnect their MCP tools automatically — no restart needed.
  echo "medley: engine ${VERSION} installed — the background service will switch to it now; your Medley tools reconnect automatically." >&2
else
  echo "medley: engine ${VERSION} ready." >&2
fi
exit 0
