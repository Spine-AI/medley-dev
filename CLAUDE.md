# CLAUDE.md — medley-dev (dev channel) contributor guide

This is the **dev channel** copy of the public Medley plugin. It is a hand-maintained copy of
`Spine-AI/medley`; the mission engine still lives in the private `Spine-AI/medley-engine` repo and
ships as a signed binary from the R2 CDN (`engine.getmedley.ai`). The only functional difference
from stable is `plugin/engine/version`, which pins an `X.Y.Z-dev.N` prerelease.

Design + rationale: `docs/superpowers/specs/2026-07-28-medley-dev-channel-design.md` in the engine repo.

## Hard rules

- **Never rename the plugin.** `plugins[0].name` in `marketplace.json` and `name` in `plugin.json`
  MUST stay `medley`. The engine identifies its own MCP server by the `plugin_medley_medley` key
  (`engine/services/host-mcp.ts` `isMedleyServer`, `host-mcp-writer.ts` `RESERVED`) and both skills
  hardcode the `mcp__plugin_medley_medley__` tool prefix. A rename makes the engine inject the
  orchestrator into its own workers. Only the *marketplace* is named `medley-dev`.
- **Never commit engine source or the built bundle here.** Same rule as stable.
- **Only five files may differ from stable:** `.claude-plugin/marketplace.json`,
  `plugin/.claude-plugin/plugin.json`, `plugin/engine/version`, `README.md`, `CLAUDE.md`.
  Everything else must stay byte-identical. Check with:
  `diff -r --exclude=.git ../medley . | grep -v '^Only in'`
- **`plugin/engine/version` uses the `X.Y.Z-dev.N` form only.** `ensure-engine.sh`'s `vsort_desc`
  normalizes the literal `-dev.`; `-beta.`/`-rc.` would misorder and strand the engine-path pointer.
- **Dev cuts never touch the stable repo's pin.** That pin is the only path from a dev build to a
  stable user.

## How the engine is found (the one real mechanism)

A marketplace install **copies** the plugin into a read-only cache (`~/.claude/plugins/cache/...`),
**forbids `../` traversal** outside the plugin dir, and runs **no** install. So the engine is not
shipped in the plugin — a self-contained **binary** is downloaded on first session into the
persistent, writable `${CLAUDE_PLUGIN_DATA}/bin` dir. No auth, no Node, no npm.

- `scripts/resolve-engine.sh` — pure resolver. Order: `$MEDLEY_ENGINE` (dev, `.cjs` or binary) →
  `${CLAUDE_PLUGIN_DATA}/bin/medley-engine-<version>` → `~/.medley/engine-path` cache.
- `scripts/ensure-engine.sh` — SessionStart bootstrap. Reads `engine/version`, maps `uname`
  → asset (`medley-engine-darwin-arm64` — Apple Silicon only; x86_64 fails soft with a
  "requires arm64 / relaunch out of Rosetta" message), `curl`s it + `SHA256SUMS` from the R2 CDN
  (`engine.getmedley.ai/v<version>`) — falling back to this repo's GitHub Release —
  verifies the checksum, `chmod +x`, caches it. Advances the `~/.medley/engine-path` cache
  (`record_engine_path` — monotonic, never downgrades) and keeps the two newest binaries, pruning
  older ones (the engine deletes the rest down to one after it rolls — see engine `runDaemon`). No-ops
  for workers (`MEDLEY_WORKER=1`) and the dev override. Fails soft (session still starts).
- `scripts/mcp-headers.sh` — the `.mcp.json` **`headersHelper`** (see below). Emits the Bearer token
  for the daemon's `/mcp` (read-or-create from the stable `<dataDir>/mcp-token`, shared with the
  engine), nudges the daemon awake if the port isn't answering (cold-start bridge), and tags a worker
  session with `X-Medley-Worker`. Prints one JSON header object; must stay fast (10s CC budget).
- `scripts/run-engine.sh` — the **stdio fallback** transport (`run-engine.sh mcp` → the engine's
  `mcp` proxy) for Claude Code older than the http/`headersHelper` baseline, and the binary resolver
  `mcp-headers.sh` reuses. Not on the default path.
- `~/.medley/engine-path` — written ONLY by `ensure-engine.sh` (`record_engine_path`, which only ever
  ADVANCES it — so a stale older-pin session, e.g. a concurrent Claude Code window on a prior plugin
  cache, cannot downgrade the cache) so the **statusline** (wired via `settings.json`, where
  `${CLAUDE_PLUGIN_DATA}` is unset) can still find the engine. `session-start.sh` deliberately does
  NOT write it: a second, unguarded writer defeated the no-downgrade guard (that was the "engine-path
  stuck on an old version after `/plugin update`" bug).
- `~/.medley/state/update.json` — a download breadcrumb `ensure-engine.sh` writes while fetching a
  new engine (`{"state":"downloading","version","since"}`, epoch-ms; removed when the download
  settles). `statusline.sh` reads it — and the engine daemon's `.rolling` roll marker (60s freshness)
  — on a fast, engine-free path *before* delegating to `status --statusline`, so `/plugin update`
  surfaces `medley ▸ ⟳ downloading engine v…` / `medley ▸ ⟳ updating engine…`.

**`.mcp.json` is a DIRECT HTTP MCP server** (`type:"http"`, `url http://127.0.0.1:8730/mcp`) — Claude
Code talks straight to the shared daemon's `/mcp`, so CC's native HTTP auto-reconnect rides out a
daemon roll (engine auto-update) and the tools survive; the old per-session stdio proxy died on a
roll and broke them. The repo rides a **static** `X-Medley-Repo-Raw: ${CLAUDE_PROJECT_DIR}` header
(CC interpolates it; the `headersHelper` does NOT get `CLAUDE_PROJECT_DIR`); the token rides the
`headersHelper`. Requires the loopback port reachable — a stale pf redirect from an old
`service dashboard --setup --portless` breaks `127.0.0.1:8730`; clear it with
`medley-engine service dashboard --teardown` (`doctor` flags this). To revert to stdio for an old CC,
set `.mcp.json` back to `{ "command": "bash", "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/run-engine.sh","mcp"] }`.

Because paths must not leave the plugin dir after caching, **never** reintroduce a `../dist`
reference in a shipped file — always go through the resolver.

## Develop & test

- **Against a local engine build** (from the private repo): build it, then
  `MEDLEY_ENGINE=/path/to/medley-engine/dist/medley-engine.cjs claude --plugin-dir ./plugin`.
- **Installed mode** (what users get): `/plugin marketplace add <local path or Spine-AI/medley>` →
  `/plugin install medley` → new session downloads the engine binary into `${CLAUDE_PLUGIN_DATA}/bin`.
- **Validate** before pushing: `claude plugin validate ./plugin --strict`. Shellcheck the
  `scripts/*.sh`.

## Layout

```
.claude-plugin/marketplace.json   the "medley" marketplace (lists this plugin, source ./plugin)
plugin/.claude-plugin/plugin.json manifest (identity metadata: name, version, author, license, …)
plugin/.mcp.json                  http MCP server → daemon /mcp (headersHelper: scripts/mcp-headers.sh)
plugin/hooks/hooks.json           SessionStart/PreCompact → session-start.sh; PreToolUse gate
plugin/scripts/                   {resolve,ensure,run}-engine.sh, session-start.sh, statusline.sh,
                                  edit-conflict-gate.py
plugin/engine/version             engine version pin (release-managed)
plugin/skills/mission|dashboard   the /mission and /dashboard skills (+ runtimes/ routing guides)
```
