# Runtime: cursor (Cursor CLI)

Cursor's coding agent, running locally with direct filesystem access. **The model-flexible
generalist.**

## Strongest at
- **Model-flexible generalist work** — its default `auto` mode account-routes each request to a
  current frontier model (GPT, Claude, or Gemini family) chosen by Cursor's own routing, rather
  than committing to one model family up front.
- **General-purpose implementation and editing** across a wide range of task shapes, since `auto`
  draws on whichever frontier model fits best rather than being locked to one vendor's strengths
  or blind spots.
- **Everyday feature work and bug fixes** in codebases of small-to-moderate size — the common case
  Cursor's own product is built and tuned around.
- **Resilience to any single model's downtime or degraded quality** — `auto` can route around a
  bad day for one provider.

## Assign a worker to cursor when its job is
- a typical, well-scoped feature or bugfix without a strong reason to prefer a specific model
  family's known strength,
- work where model diversity is valuable (e.g. running the same task on cursor alongside
  claude-code/codex as a second opinion),
- the user has explicitly asked for Cursor.

## Weaker at (relative to claude-code/codex)
- No control over exactly which underlying model handles the task in `auto` mode — if a task
  specifically needs one model family's known edge (e.g. Claude's repo-scale refactor quality or
  Codex's terminal-loop reliability), assign that runtime directly instead.
- Less established track record on structured coding benchmarks than the frontier-native
  runtimes, since its own agent harness sits on top of whichever model it's routed to.
