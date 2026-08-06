# Runtime: pi (Pi coding agent)

The Pi coding agent, running locally with direct filesystem access. **The bring-your-own-harness
minimalist** — a small, self-extensible agent on whatever model the user configured in Pi itself.

`pi` appears in the pool **only when the user already has it installed** (both the `pi` agent and its
`pi-acp` adapter). If it's in your rubric, they chose to have it — but it is also the runtime with the
fewest guarantees, so the shape of the work matters more here than anywhere else.

## Strongest at
- **Additive, self-contained implementation** — a minimal four-tool harness (`read`, `write`, `edit`,
  `bash`) with no sub-agents and no plan mode. That thinness is the point: it does exactly what the
  brief says on the files the brief names, with little interpretive drift.
- **Model diversity on the user's own terms** — Pi runs on whatever the user signed it into
  (an Anthropic, OpenAI, or GitHub Copilot subscription, or any of a dozen provider API keys). A pi
  worker is a second opinion from a model family Medley may not otherwise have in the pool.
- **Work the user wants kept on their own provider account** rather than routed through the
  subscriptions the other runtimes use.
- **Tasks the user explicitly asks Pi for.**

## Assign a worker to pi when its job is
- a well-specified, additive change — writing a new file, adding a test, filling in an
  implementation whose shape is already decided,
- a second-opinion arm alongside claude-code/codex where model diversity is the point,
- the user has explicitly asked for Pi.

## Do NOT assign a worker to pi when
- **the task needs an app / MCP tool.** Pi's ACP adapter accepts MCP server params but never wires
  them through to Pi, so a pi worker reaches *nothing* — never put slugs in a pi task's `mcps`. (The
  engine hard-routes app-bound tasks away from pi anyway; don't fight it.)
- **the task is destructive or wide-reaching** — deletions, `rm -rf`, force pushes, dependency
  surgery, schema migrations, anything you'd want a human to see before it lands. See the approvals
  note below. Send these to claude-code or cursor.
- **the task needs to ask the user something.** Like cursor, a pi worker has no first-class
  ask-the-user channel; unlike cursor it cannot even surface uncertainty by tripping an approval gate,
  because there is no gate. Genuinely ambiguous work belongs on claude-code or codex.

## Approvals — pi is not gated, at all
Pi ships **no permission system** for filesystem, process, network, or credential access, and Medley
adds no sandbox for it. So:

- `claude-code` / `cursor` — **approval-gated**: risky ops pause and the user resolves them.
- `codex` — **sandboxed**: no per-tool gate, but confined to its worktree with network off.
- `pi` — **neither**: it runs with the user's full permissions. Its worktree is the only boundary.

A `guarded` posture on a pi task is guarded in name only. The engine says so in the plan table when
that combination appears; **say it to the user too**, in your own words, when you present the plan.

## Weaker at (relative to claude-code/codex)
- **No model control from Medley.** Pi picks its own model through its own `/model` setting — never
  set `model` on a pi task; it cannot reach the CLI and would only make the plan table lie.
- **No reasoning-effort control** — like every ACP runtime except opencode, there is nowhere to put it.
- **No MCP, no Medley worker tools** — no app access and no ask-user channel (see above).
- **A community adapter in the path.** `pi-acp` is a third-party ACP bridge, not a first-party
  integration like the Cursor or OpenCode ACP modes, so it is the least battle-tested link in the
  chain. Prefer a frontier-native runtime for anything the mission genuinely depends on.
