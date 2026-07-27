# Changelog

All notable changes to the Medley plugin are documented here. This project adheres to
[Semantic Versioning](https://semver.org/). The plugin version tracks the engine version it pins.

## [0.6.4] — 2026-07-21

Plugin-only maintenance release (tracks engine v0.6.3 — no engine change). Two `session-start.sh` /
`statusline.sh` improvements:

### Changed
- **The statusline is now auto-configured for every user.** `statusLine` can't be shipped in a
  plugin manifest (Claude Code silently ignores it), so `session-start.sh` writes it into
  `~/.claude/settings.json` on first session, pointing at the stable `~/.medley/statusline.sh` copy it
  refreshes each run (survives plugin-cache pruning). It heals an older medley statusline that still
  points into the versioned plugin cache, never touches a statusline you configured yourself, and
  never re-adds one you removed. A full uninstall strips it back out.
- **The statusline slow path now serves a short-TTL per-repo cache.** Claude Code re-invokes the
  statusline on a ~300ms throttle and each call cold-started the engine binary + opened SQLite; it now
  serves a cached line (a file read) within `MEDLEY_STATUSLINE_TTL` seconds (default 2, `0` disables),
  cold-starting the engine only on a miss. The cache is keyed and verified per repo, so one repo's
  mission can never appear in another. The engine-free update-state fast path is unchanged.

### Fixed
- `session-start.sh` initialized the `--suggest` gate (`OFFERED`) so the SessionStart starter menu is
  emitted as intended (it was previously suppressed).

## [0.4.10] — 2026-07-16

Tracks engine v0.4.10 — steadier auto-updates, a redesigned Settings dashboard, and a
contract-anchored mission review loop (see the engine changelog for the full list). This release
also changes the plugin scripts:

### Changed
- **`mcp-headers.sh` sends an `X-Medley-Engine-Pin` header** (the plugin's pinned engine version) so
  the daemon can detect it's serving an older engine than the session expects and roll forward — the
  session side of the new version handshake. Worker sessions send it too, but a worker never triggers
  a roll of its own parent daemon.
- **`ensure-engine.sh` writes `~/.medley/engine-path` atomically** (tmp + rename) so the daemon's new
  stable launcher can never read a torn path.
- **`statusline.sh` surfaces `medley ▸ ⟳ update v… pending`** while a downloaded engine upgrade waits
  for the shared daemon to go idle before rolling (updates no longer interrupt an active mission).

## [0.4.7] — 2026-07-15

Tracks engine v0.4.7 — no plugin-script change. Engine-side dashboard cleanup for the free
OpenRouter tier: the topbar credit pill is removed (usage lives in Settings only), the
Medley-free ↔ your-own-key switch is fixed (instant, with an Edit-key flow instead of an
always-open input), the model picker is uncapped (full open-source catalog), and bringing your
own key now unlocks the full OpenRouter catalog (closed models too). See the engine changelog.

## [0.4.6] — 2026-07-15

### Fixed
- **`~/.medley/engine-path` could get stuck on an older engine after `/plugin update`.**
  `session-start.sh` was a second, unguarded writer of the cache; it overrode `ensure-engine.sh`'s
  advance-only guard, so a stale older-pin session (a concurrent Claude Code window still on a prior
  plugin cache) stamped the pointer — and, via the daemon prewarm, the shared daemon itself — back to
  an older version. `ensure-engine.sh` (`record_engine_path`) is now the sole, monotonic writer.
- Pairs with engine v0.4.6, which refuses to roll the shared daemon to an older version and prunes
  superseded engine binaries down to a single one (see the engine changelog for engine-side detail).

## [0.4.1] – [0.4.5] — 2026-07-15

Plugin releases tracking engine v0.4.1–v0.4.5; the engine-side changes (daemon/launchd boot fixes, the
crash-loop self-heal, and the free OpenRouter tier) are documented in the engine changelog. The one
notable plugin-script change in this range shipped in v0.4.4: `ensure-engine.sh` gained the
keep-two-newest binary prune and the advance-only `record_engine_path` engine-path cache.

## [0.4.0] — 2026-07-14

### Changed
- **One shared daemon for every repo (was one daemon per repo).** All Claude sessions now talk to a
  single background daemon over `http://localhost:8730`, backed by one shared SQLite DB; each session
  declares its repo on the wire, so the daemon keeps every repo's missions isolated by project. No
  plugin-script change was needed — the `mcp` proxy sends the repo header itself.
- **Unified dashboard.** The web dashboard now lists every repo's missions in one place, and
  `dashboard_url` deep-links straight to your current mission (`?mission=…`); the first
  `mission_start` auto-opens that deep link. `/mission` + `/dashboard` docs updated.

## [0.1.0] — unreleased

### Added
- Initial public release of the Medley plugin, split out from the private engine repo.
- `/mission` and `/dashboard` skills.
- **Persistent engine daemon.** The engine now runs as a per-repo background daemon that
  outlives Claude sessions; `.mcp.json`'s `mcp` command is a thin proxy that lazily starts it
  and, on session close, disconnects without stopping the daemon. Workers and the dashboard URL
  stay live across sessions, so `mission_resume` is only needed after a true crash/reboot
  (`/mission` and `/dashboard` docs updated accordingly). `MEDLEY_DAEMON=0` opts back into the
  legacy in-process engine.
- **Public binary distribution (no auth, no Node).** On first session the plugin downloads a
  self-contained, notarized engine binary for the platform (`medley-engine-{darwin,linux}-{arm64,x64}`)
  from this repo's public GitHub Releases into `${CLAUDE_PLUGIN_DATA}/bin`, verifies its checksum, and
  runs it directly. Replaces the previous private-npm install: removed `install.sh` (private-registry
  auth) and the `NODE_PATH` env in `.mcp.json`; `engine/package.json` → plain-text `engine/version`.
- Engine resolution (`scripts/resolve-engine.sh`): dev override (`$MEDLEY_ENGINE`) → downloaded
  binary (`${CLAUDE_PLUGIN_DATA}/bin/medley-engine-<version>`) → `~/.medley/engine-path` cache.
- Distributed via the `medley` marketplace (`/plugin marketplace add Spine-AI/medley`).
