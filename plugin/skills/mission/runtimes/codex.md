# Runtime: codex (OpenAI Codex / GPT-5.x-Codex)

OpenAI's Codex coding agent. **The terminal / execution specialist.**

## Strongest at
- **Terminal / shell / sysadmin / "make it run" work** — compiling, building, CI, environment
  & dependency setup, servers, data pipelines; running tools (compilers, test runners, solvers)
  and acting on their output. **Top family on Terminal-Bench 2.1** (GPT-5.5 ≈83%, GPT-5.3-Codex
  ≈77% — ranks #1 and #3; Claude Opus ≈75%). Best in **command-driven loops**: run it, read the
  output, fix, repeat.
- **Fast, decisive, self-contained implementation to spec** — quickest to a first working patch;
  strong at implementing a clear function/algorithm and verifying it by running code.
- **Long-horizon autonomy** on a well-specified task; **speed & cost** on bulk/mechanical work.
- **Test-suite generation and cross-language translation.**

## Assign a worker to codex when its job is
- terminal / shell / build / CI / test-running / environment or dependency setup,
- "get this concrete thing working and **verify it by running** code or tests",
- a long, well-specified, self-contained implementation,
- bulk / mechanical / repetitive changes (cheaper and faster than claude-code).

## Weaker at (relative to claude-code)
- Repo-scale, multi-file refactors and bug localization in an **unfamiliar** codebase — it can
  commit to the wrong file before fully exploring the repo.
- Open-ended / ambiguous design work and the most rigorous code review / verification.
