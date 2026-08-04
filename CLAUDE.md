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
  orchestrator into its own workers. Only the *marketplace* is named `medley-dev`. The Codex
  manifest keeps the same `name`, and the two manifests must not drift on `name` or base `version`.
- **Never commit engine source or the built bundle here.** Same rule as stable.
- **CODEX-HOST SUPPORT GRADUATED TO STABLE on 2026-07-29** (stable `medley` commit `ebe0738`, engine
  v0.8.9). The standing exception below is retired: every Codex file listed in it now exists in
  `Spine-AI/medley` too, so this repo is back to differing from stable for **channel reasons only** —
  `.claude-plugin/marketplace.json`, `.agents/plugins/marketplace.json`,
  `plugin/.claude-plugin/plugin.json`, `plugin/.codex-plugin/plugin.json`, `plugin/engine/version`,
  `README.md`, `CLAUDE.md`, plus the `medley-dev` marketplace name baked into `uninstall.sh`'s
  `MARKET`, `scripts/codex-dev-install.sh`'s `MARKETPLACE`, and the test fixtures that mirror them.
  Check with: `diff -r --exclude=.git ../medley . | grep -v '^Only in'` — anything outside that set is
  drift, and NEW work still starts here and folds back on the next graduation.
- **The pin may sit BEHIND stable between dev cuts.** `plugin/engine/version` is `0.8.8-dev.3` while
  stable is on `0.8.9` — those two builds are code-identical (0.8.9 graduated dev.3), so re-pinning
  would only cost dev users an 87MB no-op download. Bump it on the next real prerelease.

  Codex-only files (graduated — kept in sync with stable, no longer this repo's alone):
  - `plugin/.codex-plugin/plugin.json`, `.agents/plugins/marketplace.json`
  - `plugin/scripts/medley-mcp.sh` — fixed-path MISSION launcher (no plugin env on that host).
    Resolution is deliberately version-TOLERANT (`~/.medley/engine-path` = newest installed), which is
    exactly why the gateway is not routed through it — see below.
  - `plugin/scripts/mcp-gateway.sh` + `test_mcp_gateway.sh` — one file, TWO install locations. From
    `<plugin>/scripts/` it reads the manifest pin (Claude Code); `session-start.sh` also installs it as
    `~/.medley/bin/medley-gateway`, where `$DIR/..` holds no `engine/version` so it falls through to the
    fixed-path breadcrumbs `~/.medley/codex-engine-pin` + `codex-plugin-data` (Codex). That keeps ONE
    pin-STRICT rule on both hosts, which is the whole point: an older binary silently ignores
    `mcp --gateway` and serves the ORCHESTRATOR under the gateway's name — duplicate mission tools.
    The breadcrumb holds the **pin value, never a plugin root**: a root points into the VERSIONED Codex
    cache that Codex renames + prunes, i.e. the same dangling-path bug that made `session-start.sh`
    exit 127. A stale pin can only fail closed. Claude Code must never write these breadcrumbs — its
    data dir may belong to a different CHANNEL's plugin.
  - `plugin/scripts/mission-watch-gate.py` + `test_mission_watch_gate.py` — the Codex supervision
    **backstop**. Codex has no wake-on-exit, so its agent supervises by LOOPING `mission_wait` inside
    one long turn (viable only because Codex, unlike Claude Code, accepts user input mid-turn). This
    hook exists for the one thing that loop can't guarantee — a model leaving it early — and blocks
    turn-end to push the agent back in. It reads on-disk state only: no engine, no daemon, no `watch`
    subprocess (the earlier version held the turn up to 25s to re-deliver a digest `mission_wait`
    already returns instantly). No-ops on Claude Code, where the background watcher owns supervision.
  - `plugin/skills/mission/hosts/{claude-code,codex}.md` — per-host supervision rationale, read on
    demand. `mission_start`'s response is the authoritative instruction (the engine knows the host);
    these files exist so `SKILL.md` doesn't make every session pay for the other host's rules.
  - `plugin/scripts/strip-codex-config.py` + `test_strip_codex_config.py` — teardown of the
    `[plugins."medley@…"]` / `[marketplaces.…]` / `[hooks.state."medley@…"]` tables in
    `~/.codex/config.toml`
  - `scripts/codex-dev-install.sh`, `docs/codex/`

  Shared files that now diverge (fold these back on graduation):
  - `plugin/hooks/hooks.json` — the `Stop` entry, plus `apply_patch|spawn_agent` on the PreToolUse
    matcher
  - `plugin/scripts/edit-conflict-gate.py` + its test — `apply_patch` envelope parsing and
    `SPAWN_TOOLS`. (The `Bash` branch needed **no** change: Codex aliases its shell tool to
    `tool_name: "Bash"` with the command in `tool_input["command"]`, so the read-only allowlist
    already applied there.)
  - `plugin/scripts/session-start.sh` — the `medley-mcp` + `medley-gateway` launcher installs, the
    Codex-gated gateway breadcrumbs, AND the host gate that skips the Claude-only
    `~/.claude/settings.json` statusline autowire
  - `plugin/scripts/uninstall.sh` — dual-host teardown; also fixes this repo's own Claude paths,
    which were inherited from stable and pointed at the *stable* channel's dirs
  - `plugin/scripts/test_statusline_autowire.sh` — Codex host-gate cases
  - `plugin/skills/mission/SKILL.md`, `plugin/skills/dashboard/SKILL.md` — dual tool-prefix and host
    branches (mission also carries the `mission_plan_change` → `mission_steer` fix, which is
    host-agnostic and still owed to stable)

## Codex hook trust — the thing that will waste your afternoon

Measured on 0.145.0, because none of it is documented:

- A hook's `trusted_hash` in `[hooks.state."<plugin>@<market>:hooks/hooks.json:<event>:i:j"]` covers
  the hook's **command string** — not its matcher, and not the script's contents. So editing a
  matcher or a script body ships silently; changing a hook **command** invalidates trust.
- **An untrusted hook is skipped with no error.** Not a warning, not a log line — nothing. If a hook
  "isn't running", check for its trust entry before you debug the script.
- `codex exec` fires `SessionStart` and `PreToolUse` but **never `Stop`**, and never starts plugin MCP
  servers (V6). Trust for an event that never fires is never granted, so `stop:0:0` cannot appear from
  a headless run. **`exec` is not a valid proxy for the TUI** — the same lesson V6 taught for MCP now
  applies to hooks.
- A same-version `codex plugin add`, and even a full `plugin remove` + `add`, did **not** materialise
  trust for a newly added `Stop` entry; the existing four entries also survive a `remove`. Assume new
  hook events need an interactive TUI session to be trusted.
- **Never cachebust the Codex manifest**, despite what the `plugin-creator` skill says. Codex names
  the cache dir after the version the **source** manifest declares and reconciles any disagreement by
  re-materializing and pruning — so a `+codex.<ts>` install directory is renamed out from under the
  next session, dangling every absolute path it bound at start. A local marketplace re-copies on a
  plain `codex plugin add` anyway (verified), so the suffix buys nothing and costs a broken session.
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
- **Validate** before pushing: `claude plugin validate ./plugin --strict`, the
  `plugin/scripts/test_*.sh` / `test_*.py` suites, and **`shellcheck -S info plugin/scripts/*.sh`**.
  Use that exact severity flag: CI runs a bare `shellcheck plugin/scripts/*.sh`, and apt's build
  reports **info**-level findings while brew's default threshold hides them — so a bare local run went
  green on an `SC2015` (`A && B || C`) that failed CI. Prefer `if/then` over `A && B || C` regardless.

### Under Codex CLI (0.145+)

Codex has no `--plugin-dir`; it only loads plugins it has **copied** into
`~/.codex/plugins/cache` from a configured marketplace. So the loop is bump-cachebuster → reinstall →
**new thread** (tools bind at thread start), wrapped up as:

```
scripts/codex-dev-install.sh                       # downloaded engine
scripts/codex-dev-install.sh ../medley-engine/dist/medley-engine.cjs   # local build
scripts/codex-dev-install.sh --clear-engine
```

The local-build pin is `~/.medley/engine-override`, **not** `$MEDLEY_ENGINE`: a Codex plugin MCP
server inherits no session environment at all (measured — see `plugin/scripts/medley-mcp.sh`), which
is also why the manifest launches `~/.medley/bin/medley-mcp` rather than anything under the plugin.

**There is no `/mission` on Codex.** Codex plugins cannot contribute slash commands (the manifest
has no `commands` key; `/` is built-ins only, and Codex's import flow treats "Skills" and "Slash
commands" as separate categories). Skills use the `$` prefix and are namespaced by plugin, so the
invocation is **`$medley:mission`** / `$medley:dashboard` — or just state the goal, since Codex also
triggers a skill on description match. Skills load by progressive disclosure: the model reads
`SKILL.md` off disk when it picks one, so the first invocation pays a file read.

**Editing `plugin/` does nothing until you reinstall** — Codex runs its cache copy, never the source.
`codex plugin list` shows the *source* version, so it will happily report a version you are not
running; `ls ~/.codex/plugins/cache/medley-dev/medley/` is the honest check. Starting a new thread
does not re-sync either. A reinstall re-copies from source even at an unchanged version.

**The cache dir name must always equal the source manifest's version.** Codex renames it to match at
session start and prunes the loser. When they disagree, a session bound to the old directory loses
every path it captured: `session-start.sh` fails to exec (**hook exited with code 127**) and
`edit-conflict-gate.py` makes python exit **2** — which is exactly the "PreToolUse denied" signal, so
a missing file surfaces as a *blocked tool call* with a Python traceback as the denial reason. If you
ever see that pair, the cache dir was renamed mid-session; do not go looking for a hook bug.

**The engine updates itself, same as on Claude Code.** Codex gives plugin *hooks* a real writable
data dir (`~/.codex/plugins/data/medley-medley-dev`, mapped onto `CLAUDE_PLUGIN_DATA`) and fires
`SessionStart`, so `ensure-engine.sh` downloads whatever `plugin/engine/version` pins and the daemon
rolls to it. Bumping the pin therefore needs no Codex-specific step — reinstall, new thread, done.
But `~/.medley/engine-path` is **shared with Claude Code**, so a dev-channel Codex session drags the
Claude statusline and the launchd daemon trampoline onto the dev engine too. One `~/.medley`, one
daemon, one port: do not run Codex and Claude Code against different engine builds at the same time.

## Uninstall — three paths, one list

Neither host has a plugin-uninstall lifecycle hook, so `/plugin uninstall medley` /
`codex plugin remove` clears only that host's registry + cache. Everything else is cleaned up by one
of three paths, and all three read the SAME classification from the engine's
`engine/services/purge-plan.ts`:

1. **Automatic (the default).** The running daemon is the only actor that survives the removal (its
   binary's inode outlives the unlink), so it detects the orphaned state on its 30s sweep
   (`orphan-teardown.ts`) and tears itself down: bootout its LaunchAgent, purge every regenerable
   artifact (~250MB — both hosts' downloaded binaries, the trampoline, the TCC-stable link, the shims
   and breadcrumbs), then exit. Mission history / `config.toml` / BYOK keys are KEPT — unless there is
   nothing worth keeping (no missions, no keys), in which case `~/.medley` goes too and a try-once
   install leaves zero trace. `MEDLEY_ORPHAN_PURGE=0` tears down without purging.
2. **`medley-engine service uninstall --all [--keep-data]`** — the same purge plus hosts/pf teardown.
3. **`plugin/scripts/uninstall.sh`** — that, plus each host's cache/marketplace, the `~/.codex/config.toml`
   tables, the shell alias and the `settings.json` statusLine. ⚠️ `MARKET` scopes the cache/marketplace/
   config.toml cleanup to THIS channel, but `~/.medley` and the plugin-data dirs are SHARED, so a full
   (non-`--keep-data`) run from either channel removes the other's mission history too.

Five things to keep in mind when editing any of it:

- **The trigger requires BOTH hosts.** One shared daemon serves Claude Code and Codex and both
  channels, so `installedOnAnyHost` must read `~/.claude/plugins/installed_plugins.json` AND
  `~/.codex/config.toml`, matching the plugin name exactly (`key.split('@')[0]`, and the
  `[plugins."medley@` prefix). Consulting only Claude Code's registry — which it used to — tore the
  daemon down out from under a live Codex install.
- **The list is an allowlist-to-DELETE.** Anything unclassified is KEPT. A file a future engine version
  starts writing then survives an uninstall it was never classified for; the reverse polarity risks
  someone's mission history. A test asserts `keep ∩ purge = ∅`.
- **Plugin-data dirs are claimed by OWNERSHIP, never by name.** A dir is `<plugin>-<marketplace>`, so a
  third-party `medley-foo@bar` matches a `medley-*` glob. `isOwnedDataDir` requires a
  `bin/medley-engine-*` inside. (The old code instead derived one dir from the running binary and gated
  it on `basename === 'medley-medley'`, which stranded ~83MB on the dev and inline channels.)
- **`uninstall.sh` keeps a hand-written copy of the list** — it must work when no binary resolves at
  all, which is exactly when a user needs it. It prefers `service purge-plan --paths` (tab-separated so
  no jq/python is required) and falls back to two marked heredocs. The engine's
  `__tests__/purge-plan.test.ts` diffs the two and fails on drift; it skips when the plugin repo isn't
  checked out beside the engine, so run the engine suite after touching either file.
- **Ordering is load-bearing.** The LaunchAgent is removed FIRST and the purge is abandoned if that
  fails: without the plist gone, KeepAlive relaunches into a purged install and execs a trampoline
  whose target we just deleted. Leaving everything in place keeps the understood failure mode (exit 78,
  launchd throttles).

## Layout

```
.claude-plugin/marketplace.json   the "medley" marketplace (lists this plugin, source ./plugin)
.agents/plugins/marketplace.json  the same, for Codex (root = repo root; add with `codex plugin
                                  marketplace add .`)
scripts/codex-dev-install.sh      Codex dev loop (validate → cachebust → codex plugin add → restore)
plugin/.claude-plugin/plugin.json manifest (identity metadata: name, version, author, license, …)
plugin/.codex-plugin/plugin.json  Codex manifest — inline mcpServers, no `hooks` key (its validator
                                  rejects one; the runtime finds hooks/hooks.json by path anyway)
plugin/.mcp.json                  http MCP server → daemon /mcp (headersHelper: scripts/mcp-headers.sh)
plugin/hooks/hooks.json           SessionStart/PreCompact → session-start.sh; PreToolUse gate;
                                  Stop → mission-watch-gate.py (Codex supervision + the Claude Code
                                  composer rung); SessionEnd → session-end-marker.py;
                                  UserPromptSubmit → session-catchup.py
plugin/scripts/                   {resolve,ensure,run}-engine.sh, session-start.sh, statusline.sh,
                                  edit-conflict-gate.py, medley-mcp.sh (installed to the fixed path
                                  ~/.medley/bin/medley-mcp for hosts with no plugin env — Codex),
                                  mcp-gateway.sh (the gateway launcher; ALSO installed to the fixed
                                  path ~/.medley/bin/medley-gateway, pin-strict via breadcrumbs),
                                  mission-watch-gate.py (Stop hook: Codex supervision watcher, and
                                  on Claude Code the one rung that blocks an agent from going idle
                                  while the dashboard composer is owed a reply — it nudges the agent
                                  to re-arm the watcher rather than carrying the message, since only
                                  a channel that can atomically claim it may deliver it),
                                  session-end-marker.py + session-catchup.py (the same channel's
                                  closed-terminal and reopened-terminal halves: the marker lets the
                                  engine resume the session, the catch-up shows you what it said),
                                  session-mission-binder.py (binds a session to the live mission),
                                  uninstall.sh (complete teardown — run BEFORE the host's own
                                  uninstall, which deletes the cache this script lives in),
                                  strip-codex-config.py (uninstall: ~/.codex/config.toml tables)
plugin/engine/version             engine version pin (release-managed)
plugin/skills/mission|dashboard   the /mission and /dashboard skills (+ runtimes/ routing guides and
                                  mission/hosts/ per-host supervision guides, both read on demand)
```
