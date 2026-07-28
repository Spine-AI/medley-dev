# Codex host support — phased checklist

Tracker for making `medley-dev/plugin/` run as a first-class **Codex CLI 0.145+** plugin, with Codex
hosting the mission agent and Codex/GPT workers as the routing default — while the same folder keeps
working unchanged under Claude Code.

Design + rationale: `docs/superpowers/specs/2026-07-28-codex-host-design.md` **in the engine repo**.
Read it before ticking anything here; this file is the work list, not the reasoning.

**Status:** Phase 0 answered on the blocking questions, Phase 1 installable (2026-07-28).

- **V1, V2, V6 answered.** V6: plugin MCP servers **do** start in the Codex TUI — `codex exec` never
  starts them, so `exec` is not a valid proxy for the TUI in any future verification.
- **V7 answered — and it decided the transport.** The env-dump probe returned the MCP server's
  *complete* environment: `HOME LANG LOGNAME PATH PWD SHELL SHLVL TERM TMPDIR USER`
  `__CF_USER_TEXT_ENCODING`. **No plugin variable of any kind.** The `codex` binary's strings put
  `PLUGIN_ROOT`/`CLAUDE_PLUGIN_ROOT`/`PLUGIN_DATA`/`CLAUDE_PLUGIN_DATA` inside
  `codex_hooks::engine::command_runner` — hooks only. Combined with V4 (no `${VAR}` interpolation in
  the manifest), **a Codex `mcpServers` command can never name the plugin's own directory**, so
  `run-engine.sh` is unreachable and `bash -c 'exec "$PLUGIN_ROOT/…"'` is dead in both forms.
  → **Fix: a fixed-path launcher, `~/.medley/bin/medley-mcp`** (`plugin/scripts/medley-mcp.sh`,
  installed by `session-start.sh` exactly as `~/.medley/statusline.sh` already is, for exactly the
  same "this context has no plugin env" reason). It resolves the engine from
  `$MEDLEY_ENGINE` → `~/.medley/engine-override` (dev pin; env can't be passed on this host) →
  `~/.medley/engine-path`. Verified: handshake under a byte-for-byte reproduction of the measured
  bare env returns `serverInfo {name: medley}` and all 21 tools, `contract_set` and
  `mission_plan_submit` among them.
- **Phase 1 installable:** manifest, marketplace and a dev install loop exist; both validators green;
  `codex plugin add medley@medley-dev` succeeds. Unproven: the TUI end-to-end run.

## Ground rules

- **Never rename the plugin.** `medley` in both manifests. `engine/services/host-mcp.ts:98` and
  `host-mcp-writer.ts:43` key on it; a rename makes the engine inject the orchestrator into its own
  workers.
- **Never put `hooks` in `.codex-plugin/plugin.json`.** Codex's validator rejects the key outright
  while the runtime reads `hooks/hooks.json` by path. Declaring it fails validation for no gain.
- **Claude Code must not regress.** Its http `medley` entry stays; it is what survives a daemon roll
  during engine auto-update. Every phase ends with the Claude path re-verified.
- **The two manifests must not drift.** Same `name`, same base `version`. A local Codex dev
  reinstall rewrites the Codex one to `<base>+codex.<token>` — that suffix must never be committed.
- **Both validators, every phase:**
  ```
  python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py plugin
  claude plugin validate ./plugin --strict
  ```

---

## Phase 0 — verification spike

Answer the open questions with a **throwaway** plugin. No file under `plugin/` changes in this
phase. Record each answer back into the design doc, replacing the assumption it tested.

- [x] Scaffold a throwaway Codex plugin outside this repo (scratch dir) with its own local
      marketplace — done as `medley-probe`, still installed pending V6
- [x] **V1 — `.mcp.json` double-discovery.** → **No doubling, either way.** Inline-object
      `mcpServers` registers only the inline names and leaves the root `./.mcp.json` alone;
      `"mcpServers": "./.mcp.json"` registers only the file's servers.
      **→ Ship the inline object form.** It is exactly what keeps Claude's http + `headersHelper`
      entry invisible to Codex. The string form would make both hosts read one file and break the
      dual-manifest layout.
- [x] **V2 — MCP server cwd.** → **Good.** cwd is the workspace root and `CLAUDE_PROJECT_DIR` is
      unset, so `repoRoot()`'s `process.cwd()` fallback (`engine/headless/medley-engine.ts:80`)
      resolves the right repo.
- [x] **V6 — do plugin MCP servers start at all?** → **Yes, in the TUI.** They start and (with a bad
      path) fail at handshake. `codex exec` never starts them — **`exec` is not a valid proxy for the
      TUI**; remember this for any future headless verification. Cold start therefore moves to first
      tool use, which makes the `SessionStart` hook *more* important on Codex, not less.
- [x] **V7 (WAS BLOCKING) — how does a plugin MCP server locate its own root?**
      → **It cannot.** The env dump is exhaustive and contains no plugin variable; the vars live in
      the hooks command runner. So the answer is to stop needing a plugin root: the MCP entry
      addresses the fixed path `~/.medley/bin/medley-mcp`, which `session-start.sh` installs (the
      hook *does* get `PLUGIN_ROOT`). A fixed path also survives a plugin-cache prune, which a
      versioned path would not. **Do not** inline `bash -c 'exec "$(cat ~/.medley/engine-path)" mcp'`
      in the manifest instead — it forfeits the dev-override hop and the `.cjs`-vs-binary dispatch,
      and puts launch logic in a JSON string no shellcheck ever sees.
- [x] **V3 — hook trust gate.** → **Hooks fire, and the whole Claude bootstrap works unchanged.**
      The Phase-0 "hooks never fire under exec" reading was an artifact of the throwaway probe, not a
      Codex limitation: with the real plugin installed from a configured marketplace, a single
      `codex exec` fired `SessionStart` and Codex auto-trusted all four events
      (`[hooks.state."medley@medley-dev:hooks/hooks.json:{session_start,pre_tool_use,post_tool_use,pre_compact}:0:0"]`
      appeared in `~/.codex/config.toml` with no prompt).
      **Codex supplies a real, writable plugin data dir** — `~/.codex/plugins/data/<plugin>-<marketplace>`,
      here `medley-medley-dev` — mapped onto `CLAUDE_PLUGIN_DATA`. So `ensure-engine.sh` ran, fetched
      the **pinned** `medley-engine-0.8.6-dev.1` into it, `record_engine_path` advanced
      `~/.medley/engine-path` to it, and the shared daemon rolled to that version. `session-start.sh`
      also installed `~/.medley/bin/medley-mcp` from the hook, as designed.
      **⚠️ Cross-host consequence:** `~/.medley/engine-path` is shared, so a Codex dev-channel session
      drags the Claude Code statusline and the launchd daemon trampoline onto the dev engine. This is
      the existing "never run dev and stable side by side" hazard, now reachable from a second host.
- [ ] **V4 — `$PLUGIN_ROOT` in MCP args.** Partial: `codex mcp list` renders `${PLUGIN_ROOT}`
      **un-interpolated**, weak evidence the host does not interpolate and support for the `bash -c`
      form. Unconfirmed at launch because no plugin server ever launched (V6).
      → answer: _______
- [ ] **V5 — hook matcher syntax.** Claude matchers (`Edit|Write|MultiEdit|NotebookEdit|Task|Bash`)
      name Claude tools. What does Codex do with a matcher that matches nothing — ignore, or error?
      → answer: _______
- [ ] Write all five answers into the design doc
- [ ] Remove the throwaway plugin and its marketplace entry

**Exit:** every V-question has a recorded answer. If V1 or V2 comes back badly, revisit the design
before Phase 1.

---

## Phase 1 — Codex-hosted mission agent

Goal: `/mission` runs end to end under `codex`, using today's routing.

- [x] Create `plugin/.codex-plugin/plugin.json`
  - [x] `name: "medley"`, `version` = the Claude manifest's version (strict semver), `description`,
        `author.name`
  - [x] `interface{}`: `displayName`, `shortDescription`, `longDescription`, `developerName`,
        `category`; any `websiteURL` / `privacyPolicyURL` / `termsOfServiceURL` must be absolute
        `https://`
  - [x] `defaultPrompt`: **required** (validator rejects the manifest without it). At most 3
        entries, ~50 chars each (later entries dropped, >128 chars truncated)
  - [x] **no `hooks` key**; **no `apps` key** unless a `.app.json` actually exists
  - [x] inline `mcpServers` — per V7, via the fixed-path launcher, NOT `$PLUGIN_ROOT`:
        - `medley` → `bash -c 'exec "$HOME/.medley/bin/medley-mcp" mcp'`
        - `medley_gateway` → **deliberately omitted for now.** `mcp --gateway` is pin-STRICT by
          design (`mcp-gateway.sh` refuses to fall back to `~/.medley/engine-path`, because an
          older binary silently ignores the flag and serves the ORCHESTRATOR under the gateway's
          name — duplicate mission tools, the worst failure mode for a first Codex run). The
          launcher has only `engine-path` to go on, so it cannot honour that guard.
          **Now cheap to fix, given V3:** the hook has both `CLAUDE_PLUGIN_ROOT` and a real
          `CLAUDE_PLUGIN_DATA`, so it can drop them at fixed paths (`~/.medley/codex-plugin-root`,
          `~/.medley/codex-plugin-data`) for a gateway shim to read — restoring full pin-strict
          resolution. Do that when connected apps are in scope.
  - [x] apply whatever V1 concluded about the root `.mcp.json` — inline object form, so Codex
        ignores the http+`headersHelper` entry entirely (confirmed: it sits in the installed cache
        copy and is not registered)
- [x] Make the hardcoded Claude tool namespace host-agnostic (Codex uses `mcp__medley__*`, Claude
      uses `mcp__plugin_medley_medley__*` — the engine already accepts both, these three strings did not)
  - [x] `plugin/skills/mission/SKILL.md` — preflight `ToolSearch` probe now documents both prefixes
  - [x] `plugin/skills/dashboard/SKILL.md` — same for `dashboard_url`
  - [x] `engine/services/status-render.ts:340` (**engine repo**) — recovery instruction now names both
- [x] Invert the mission skill's Codex escape hatch. It told a Codex session Medley could never
      connect; now absent tools on Codex is the recoverable "engine isn't up yet" case, and the
      terminal branch applies only to hosts that are neither Claude Code nor Codex.
- [x] Add host detection to the mission skill so it knows which tool prefix it is speaking
- [x] Describe workers as runtime-neutral rather than "fresh Claude Code sessions"
- [x] Engine tests green (1348 passed); `claude plugin validate ./plugin --strict` green
- [x] `plugin/scripts/medley-mcp.sh` + its `session-start.sh` install step (the V7 fix)
- [x] `scripts/codex-dev-install.sh` — the dev loop (validate → marketplace → `codex plugin add`)
- [x] **V5 answered, and a self-inflicted bug found and fixed with it.** First real TUI run failed
      twice: `SessionStart hook (failed) error: hook exited with code 127` and
      `PreToolUse hook (blocked)` carrying a Python "No such file or directory" for
      `edit-conflict-gate.py`. One cause, not two. The script was following the `plugin-creator`
      skill's cachebuster advice, installing to `…/<ver>+codex.<ts>/` while the source manifest was
      restored to `<ver>`. **Codex names the cache dir after the SOURCE version and reconciles a
      disagreement by re-materializing + pruning** — so the directory was renamed out from under the
      live session and every absolute path it had bound went stale. `session-start.sh` could not be
      exec'd → 127; python could not open the gate → **exit 2**, which is precisely the "PreToolUse
      denied" convention, so a missing file masqueraded as a deliberate block.
      → **Fix: no cachebuster.** A local marketplace re-copies from source on a plain
      `codex plugin add` at an unchanged version (verified with a probe file), so the suffix bought
      nothing. Re-verified: `hook: SessionStart Completed`, `hook: PreToolUse Completed`, tool ran.
      → **V5 proper:** the Claude-named matcher `Edit|Write|MultiEdit|NotebookEdit|Task|Bash` *does*
      fire on Codex tools, but `edit-conflict-gate.py` already fails open on an unrecognized
      `tool_name` (exits 0), so it no-ops. Also learned: Codex honours Claude's PreToolUse exit-code
      contract (2 = deny + stderr as reason) but **rejects `permissionDecision: allow|ask`** — only
      `deny` is supported.
- [x] Both validators green (`validate_plugin.py` needs pyyaml — the script runs it via
      `uv run --with pyyaml`)
- [x] `codex plugin add medley@medley-dev` succeeds; cache copy carries skills, hooks, scripts
- [x] Bare-env handshake proves the transport: 21 tools including `contract_set`,
      `mission_plan_submit`, `mission_start`, `dashboard_url`
- [x] **Skill invocation differs and it is not `/mission`.** Codex plugins cannot contribute slash
      commands — the manifest has no `commands` key, `/` is built-ins only, and Codex's import flow
      lists "Skills" and "Slash commands" as separate categories. Skills have their own composer
      surface (`tui/src/bottom_pane/skill_popup.rs`, `UserInput::Skill`) and the `$` prefix; Codex's
      system prompt: *"If the user names an available skill (with `$SkillName` or plain text) OR the
      task clearly matches an available skill's description, you must use that skill for that turn."*
      Names are plugin-namespaced, so it is **`$medley:mission`** / `$medley:dashboard` — confirmed
      against a live session's catalog. Skills load by **progressive disclosure** (the model reads
      `SKILL.md` on demand), so the description in frontmatter does all the routing work.
      `defaultPrompt` in the Codex manifest updated to match. Left alone deliberately: the shared
      `SKILL.md` bodies still say `/mission`, which is correct on Claude Code and harmless on Codex
      (the model has already opened the file by then).
- [ ] Run a real mission end to end in the Codex TUI (contract → plan → workers)
- [ ] Re-verify Claude Code is unaffected (`claude --plugin-dir ./plugin`, run a mission)
- [ ] V3/V5 fall out of the first TUI run: does the SessionStart hook fire (and does its trust gate
      prompt), and what does Codex do with the Claude-named `PreToolUse` matcher?

**Exit:** a mission planned and completed under `codex`.

---

## Phase 2 — Codex workers by default

Goal: a mission planned on Codex routes to `codex` workers with zero configuration.
All engine-repo work. `routing.prefer` is global config today
(`engine/models/routing.ts:118`, ordered `kimi-oss → claude-code → codex`) and one `~/.medley`
serves both hosts, so the host has to travel on the wire.

- [x] `--host <id>` flag on the engine `mcp` command (`parseMcpProxyHost`; `medley-mcp.sh` already
      forwards `"$@"`, so the Codex manifest's `mcp --host codex` now lands instead of being ignored)
- [x] `mcpProxyHeaders()` emits `X-Medley-Host` — omitted entirely when no `--host` was passed, so
      Claude Code and every older plugin stay on the no-header (= `claude-code`) path
- [x] `/mcp` handler reads `x-medley-host` onto the session (`decodeHostHeader`, in
      `models/runtime.ts` beside the type — pure, and cheap to import in a test)
- [ ] `missions` table gains a **nullable** `host` column + migration *(only needed for ROUTING —
      supervision reads the session's host directly, so this is still open for Phase 2 proper)*
- [ ] `HOST_NATIVE_PREFER` in `engine/models/routing.ts`:
      `codex: ['codex','kimi-oss','claude-code']`
- [ ] `routeRuntime()` consults it **only when the user has not set `prefer`**
      — explicit user config wins on every host; this replaces the seeded default, it does not
      layer over user intent
- [ ] Absent header behaves exactly as today (`claude-code` semantics), so older proxy binaries and
      pre-migration mission rows are unaffected
- [ ] Settings surfaces the effective order **and where it came from**, so a Codex user is not left
      guessing why tasks route to GPT
- [ ] Update the Codex manifest's `medley` entry to pass `mcp --host codex`
- [ ] Tests:
  - [ ] host-header parsing matrix (present / absent / blank / unknown), mirroring `decodeRepoHeaders`
  - [ ] `routeRuntime` table tests: explicit `prefer` beats host-native on every host; host-native
        applies when unset; absent host = `claude-code`
  - [ ] migration test: existing rows survive with `host = NULL`
- [ ] Telemetry + a view for it (engine contributor rule): mission host × resolved runtime, answering
      "does a Codex-hosted mission actually route to Codex" — plus the matching PostHog tile

**Exit:** zero-config Codex mission routes to Codex workers; a Claude Code mission on the same
machine is unchanged.

---

## Phase 3 — distribution

- [x] `.agents/plugins/marketplace.json` in this repo — `source: {source: "local", path: "./plugin"}`,
      `policy.installation`, `policy.authentication`, `category`, and top-level
      `interface.displayName`. Marketplace name `medley-dev`, matching the Claude one. Paths are
      relative to the **marketplace root** = the dir containing `.agents/`, i.e. the repo root; it is
      NOT discovered implicitly (only `~/.agents/plugins` is), hence the
      `codex plugin marketplace add <repo>` in the dev script.
- [ ] CI (`.github/workflows/validate.yml`): add `validate_plugin.py` beside the existing
      `claude plugin validate` and shellcheck
- [ ] CI guard: the two manifests' `name` and base `version` must match
- [ ] CI guard: reject any committed `+codex.` cachebuster suffix
- [ ] Release flow: the engine-pin bump updates both manifests in lockstep
- [ ] Document the local dev loop in `CLAUDE.md`:
      `update_plugin_cachebuster.py plugin` → `codex plugin add medley@<marketplace>` → **new thread**
- [ ] Amend `CLAUDE.md`'s "only five files may differ from stable" rule to cover the new files

**Exit:** `codex plugin add medley@medley-dev` installs a released build.

---

## Phase 4 — gaps and accepted losses

- [x] **Supervision — SOLVED, and not the way this file assumed.** Codex has no wake-on-exit, so the
      first cut used a `Stop` hook to hold the turn open ~25s per turn-end and hand back a digest: one
      peek per user turn, and the mission went dark in between. Measured on 0.145: Codex accepts input
      into a RUNNING turn (`turn/steer`, `steer_count` turn telemetry, `ActiveTurnNotSteerable`), which
      is the constraint Claude Code's background watcher exists to work around and which Codex does
      not have. So the agent now **loops `mission_wait` inside one long turn**, and the `Stop` hook is
      demoted to a backstop that pushes it back in if it leaves early.
      Being parked in a tool call is also what makes two engine push channels legal:
      `notifications/progress` (host-rendered live activity, zero model tokens) and
      `elicitation/create` for ⚡ items — Codex gates the latter behind `tool_call_mcp_elicitation`
      (stable/**true**), i.e. an elicitation is expected to belong to an in-flight tool call, which the
      loop satisfies by construction. Answered on the way: a Codex **subagent** is the wrong
      mechanism (`wait_agent` blocks the parent, so it buys the same held turn while paying a second
      model; `multi_agent_v2`'s mailbox and `request_user_input` are both OFF by default).
      Shipped: `--host` → `X-Medley-Host` → session host → host-shaped `mission_start`/`mission_wait`
      text, `engine/services/supervision-channel.ts`, `mission_supervision_armed` +
      `attention_elicited` telemetry, `plugin/skills/mission/hosts/*.md`.
- [ ] **Statusline — accepted loss, write it down.** Codex 0.145 has no user-supplied statusline
      (`status_line` is a TUI display pref, not a command hook). The mission deep link, the
      `⟳ downloading engine` indicator and live task counters have no Codex equivalent.
      `statusline.sh` and the `~/.medley/engine-path` cache stay Claude-only. `dashboard_url` and the
      `dashboard` skill are the substitute.
- [ ] `hooks/hooks.json` matchers: reconcile with Codex tool names per V5, or scope the gate to Claude
- [ ] `edit-conflict-gate.py` — confirm the Codex `PreToolUse` payload is compatible
- [ ] `session-mission-binder.py` — its `PostToolUse` matcher is `mcp__.*__mission_(start|resume|status|wait)`,
      which already covers both namespaces; verify against a real Codex payload
- [ ] `session-start.sh` / `ensure-engine.sh` under Codex: not on the critical path (the MCP proxy
      self-starts the daemon), but confirm they no-op cleanly rather than erroring
- [ ] **Codex-worker MCP isolation leak.** The `strictMcpConfig` equivalent is missing for
      codex/kimi/cursor workers — pre-existing, but a Codex-default setup makes it the common path
      instead of the rare one. Decide whether it blocks calling Phase 2 done.
