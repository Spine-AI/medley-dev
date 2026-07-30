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

```
/plugin marketplace add Spine-AI/medley-dev
/plugin install medley@medley-dev
```

The plugin is still *named* `medley` — only the marketplace is `medley-dev`. That is
deliberate: the engine identifies its own MCP server by the `plugin_medley_medley` key,
and renaming the plugin would make it inject the orchestrator into its own workers.

Confirm which channel you are on by the engine version — dev builds carry a `-dev.N`
suffix:

```bash
cat ~/.medley/engine-path     # …/medley-engine-0.8.6-dev.0
```

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

```
~/.claude/plugins/cache/medley-dev/medley/*/scripts/uninstall.sh   # then the host command above
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
