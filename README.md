# medley-dev — Medley's dev channel

Prerelease Medley builds for the **Medley dev team**. This is where engine and daemon
upgrades get tested end to end — through the real download, checksum, install, and
self-update path — before they reach the stable channel.

Not for general use. Stable Medley lives at [Spine-AI/medley](https://github.com/Spine-AI/medley).

## Read this before installing

The dev channel deliberately shares machine-global state with the stable channel: the
`~/.medley` data root (including the SQLite mission DB), TCP port `8730`, and the single
`ai.getmedley.daemon` LaunchAgent. It is **not** an isolated sandbox.

So the two channels **cannot be installed at the same time**. Both would fight over one
`~/.medley/engine-path` pointer and one daemon, and a dev engine would apply dev
migrations to your real mission DB. Migrations are forward-only — that is a one-way door
for that machine's mission history.

Before your first dev install:

1. Finish or abandon any in-flight mission (`mission_status` / the dashboard).
2. Back up the state dir:
   ```bash
   cp -R ~/.medley/state ~/.medley/state.backup-$(date +%Y%m%d)
   ```
3. Remove the stable channel: `/plugin uninstall medley`

Do the same backup before switching back to stable — a DB a dev build has migrated may
not open on the stable engine.

## Install

**Claude Code** — inside a session:

```
/plugin marketplace add Spine-AI/medley-dev
/plugin install medley@medley-dev
```

or from a terminal, without a session:

```bash
claude plugin marketplace add Spine-AI/medley-dev
claude plugin install medley@medley-dev
```

**Codex CLI** — requires **codex ≥ 0.142.0** (an older build fails with
`missing or invalid plugin.json` — see [Troubleshooting](#troubleshooting)). The dev channel is
developed and measured against 0.145+:

```bash
codex --version                                   # must be ≥ 0.142.0
codex plugin marketplace add Spine-AI/medley-dev
codex plugin add medley@medley-dev
```

Then start a **new thread** — Codex binds a plugin's tools and hooks at thread start. Approve
Medley's hooks the first time each fires; the `Stop` hook is how a mission keeps supervising itself
on that host. There is no `/mission` on Codex (plugins cannot contribute slash commands) — the
skills are **`$medley:mission`** and `$medley:dashboard`, or just state the goal.

The plugin is still *named* `medley` — only the marketplace is `medley-dev`. That is
deliberate: the engine identifies its own MCP server by the `plugin_medley_medley` key,
and renaming the plugin would make it inject the orchestrator into its own workers.

## Check what you're running

```bash
cat ~/.medley/engine-path                     # …/medley-engine-0.9.1-dev.2 — a -dev.N suffix means dev channel
claude plugin list                            # installed plugins (Claude Code)
codex plugin list                             # STATUS + VERSION per plugin (Codex)
ls ~/.codex/plugins/cache/medley-dev/medley/  # the copy Codex actually runs
codex mcp list | grep medley                  # medley + medley_gateway should both be listed
```

⚠️ `codex plugin list` reports the **source** manifest's version, so it will happily name a version
you are not running. The `ls` of the cache dir is the honest check.

## Update

A dev cut is a bumped `plugin/engine/version` in this repo. Refresh the marketplace snapshot, then
reinstall the plugin:

```
/plugin marketplace update medley-dev          # Claude Code
/plugin update medley
```

```bash
codex plugin marketplace upgrade medley-dev    # Codex — `upgrade`, not `update`
codex plugin add medley@medley-dev             # re-copies the source even at an unchanged version
```

On Codex, start a **new thread** afterwards. Your next session downloads the newly pinned engine
binary (checksum-verified) and the running daemon rolls itself forward onto it.

## Troubleshooting

**Codex: `Error: missing or invalid plugin.json`** — your `codex` predates 0.142.0. The Codex
manifest declares its MCP servers under the camelCase `mcpServers` key, which Codex only understands
from **0.142.0**; older builds reject the entire manifest on that one unknown key. Bisected:
0.138.0 / 0.139.0 / 0.140.0 / 0.141.0 fail, 0.142.0 → 0.147.0-alpha install. `codex plugin
marketplace add` still succeeds either way, which is what makes this look like a repo problem.

```bash
brew upgrade codex        # or: npm i -g @openai/codex@latest
codex --version
codex plugin marketplace add Spine-AI/medley-dev
codex plugin add medley@medley-dev
```

**Codex: a hook "isn't running"** — an untrusted hook is skipped silently: no error, no log line.
Trust is recorded per hook *command* in `~/.codex/config.toml` under
`[hooks.state."medley@medley-dev:hooks/hooks.json:<event>:i:j"]`, and a new hook event needs an
interactive TUI session to be granted it (`codex exec` never fires `Stop` and never starts plugin
MCP servers, so it cannot grant trust).

**Editing `plugin/` appears to do nothing** — both hosts run a *copy* in their plugin cache, never
your working tree. On Codex, `codex plugin add medley@medley-dev` + a new thread. On Claude Code,
`claude --plugin-dir ./plugin` for a live-source loop.

**Tools or skills missing mid-session** — both hosts bind a plugin's tools at session/thread start.
Start a new one.

More Codex-specific behaviour (hook-trust hashing, the cache-rename trap behind
`hook exited with code 127`) is written up in `CLAUDE.md`.

## Uninstall

Removing the plugin is enough — the orphaned background service tears itself down within about a
minute (never mid-mission), taking its LaunchAgent, its launcher and every downloaded engine binary
on every host with it. Mission history, `config.toml` and BYOK keys are kept so a reinstall resumes;
if you never ran a mission, `~/.medley` goes too.

```
/plugin uninstall medley@medley-dev          # Claude Code
codex plugin remove medley@medley-dev        # Codex
```

For an immediate, total removal run the uninstaller **first** — it ships inside the plugin, so the
host command above deletes it:

```bash
~/.claude/plugins/cache/medley-dev/medley/*/scripts/uninstall.sh   # Claude Code
~/.codex/plugins/cache/medley-dev/medley/*/scripts/uninstall.sh    # Codex
# …then the host command above
```

`--dry-run` shows the plan; `--keep-data` keeps the DB, config and keys (the service is removed
either way). ⚠️ `~/.medley` is **shared with the stable channel** — a full uninstall from either
channel removes the other's mission history too, so use `--keep-data` if both are installed.

## How a dev build reaches you

Engine prereleases are cut as `X.Y.Z-dev.N` from the private engine repo and published by
its existing release workflow to `engine.getmedley.ai/vX.Y.Z-dev.N/`. This repo's
`plugin/engine/version` is then hand-bumped to that version. `/plugin update` downloads
the binary, verifies its checksum, advances `~/.medley/engine-path`, and the running
daemon rolls onto it.

Nothing in that flow writes the stable repo's pin, so stable users are unaffected.

## Keeping this repo current

This is a hand-maintained copy of `Spine-AI/medley`. Only these files intentionally
differ:

| File | Difference |
|---|---|
| `.claude-plugin/marketplace.json` | `name` is `medley-dev`; dev-channel description |
| `plugin/.claude-plugin/plugin.json` | `homepage`/`repository` point here; `[DEV CHANNEL]` description |
| `plugin/engine/version` | the dev pin, `X.Y.Z-dev.N` |
| `README.md`, `CLAUDE.md` | this documentation |

Everything else — every script, hook, skill, and `plugin/.mcp.json` — must stay identical
to stable. `plugin/scripts/ensure-engine.sh` especially: if it ever diverges, this repo
has silently stopped being a faithful preview of what stable users will get.
