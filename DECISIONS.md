# Decisions log

One line per autonomous decision made overnight (2026-09-24), most recent last.

- No `ANTHROPIC_API_KEY` or `SAM_API_KEY` in the environment; no `MAX_RUN_USD` set. Proceeding with SAM fixtures-only (never calling SAM live) and building/testing the LLM scoring path with mocked responses only; no live Anthropic spend is possible or attempted tonight.
- Checked `Kghaffari26/agents-core` (main @ `f79b6aa`, only branch): no commit yet has both `src/agents_core/guards.py` and `src/agents_core/registry.py` — the repo is still monorepo-shaped (`core/`, package name `agents-hub`, `[tool.uv] package = false`). Doing core-independent tasks (1, 2, 6-prep, 7, 8-prep) first and rechecking periodically, per instructions.
