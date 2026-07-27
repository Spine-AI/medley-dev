# Runtime: claude-code (Claude Code)

Anthropic's Claude coding agent, running locally with direct filesystem access. **The
repo-reasoning / quality specialist.**

## Strongest at
- **Repo-scale software engineering & multi-file refactors** — holds a whole subsystem in view
  and changes it coherently. **Leads SWE-bench Verified** (Opus 4.8 ≈89% vs GPT-5.5 ≈83%) and
  SWE-bench Pro (repo-scale code editing).
- **Bug localization & fixing** — finds the root cause and produces minimal, correct, multi-step
  patches; markedly less likely to let buggy code pass than prior versions.
- **Code review / verification / security analysis** — careful, thorough critique; best at
  catching another worker's mistakes.
- **Exploratory / ambiguous / judgement-heavy work** — investigates an unfamiliar codebase
  before changing it; reliable when scope is under-specified.

## Assign a worker to claude-code when its job is
- implementing a non-trivial feature or fixing a subtle bug in real / unfamiliar code,
- refactoring across many files or reasoning about the whole codebase,
- reviewing / verifying / auditing another worker's output,
- research, analysis, writing, or other non-code deliverables (web search, MCP, and the
  user's skills are all available),
- anything quality-sensitive, ambiguous, or requiring design judgement.

## Weaker at (relative to codex)
- **Terminal-native / command-driven ops** — pure terminal coding is its weakest area (Codex
  leads Terminal-Bench). Prefer codex for shell/build/run/test-heavy work.
- Raw speed and cost-efficiency on large, mechanical batches.
