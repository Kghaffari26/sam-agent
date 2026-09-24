# Decisions log

One line per autonomous decision made overnight (2026-09-24), most recent last.

- No `ANTHROPIC_API_KEY` or `SAM_API_KEY` in the environment; no `MAX_RUN_USD` set. Proceeding with SAM fixtures-only (never calling SAM live) and building/testing the LLM scoring path with mocked responses only; no live Anthropic spend is possible or attempted tonight.
- Checked `Kghaffari26/agents-core` (main @ `f79b6aa`, only branch): no commit yet has both `src/agents_core/guards.py` and `src/agents_core/registry.py` — the repo is still monorepo-shaped (`core/`, package name `agents-hub`, `[tool.uv] package = false`). Doing core-independent tasks (1, 2, 6-prep, 7, 8-prep) first and rechecking periodically, per instructions.
- Relevance pre-score (§5.3) keyword signals (title/description) use `profile.keywords` for both contracts and grants, not `profile.grant_keywords` (which the spec scopes to building Grants.gov search queries, §3.2). Grants still get NAICS/PSC = 0 as the spec expects, relying on the keyword and agency signals.
- `api.grants.gov` is blocked by this environment's egress policy (proxy returns 403 on CONNECT), so the "use Grants.gov live to record fixtures" instruction can't be carried out here. Built `tests/fixtures/grants/` by hand instead, matching the documented `search2`/`fetchOpportunity` response shapes from SPEC_GRANTS.md §3.2 as closely as possible. Flagged in STATUS.md for a real recording pass once network access allows it.
