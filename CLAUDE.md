# sam-agent

The grants/contracts finder agent, one repo in a multi-repo split (see
`docs/specs/SPEC_GRANTS.md`, written for a monorepo — where it and this file
disagree on repo layout, this file wins; its §2-§13 content, especially the
§6 JSON output contract, still governs).

## Repo split

- **This repo (`sam-agent`)**: the `grants` agent only — `agents/grants/`,
  `config/`, its tests and evals.
- **`agents-core`**: shared code (`agents_core` package) — LLM batch client,
  HTTP with request budgets, cost tracking, publish helpers, the number
  guard, the agent registry/runner. Installed as a git dependency, pinned to
  a commit SHA (see STATUS.md for the currently pinned SHA, or "not yet
  installed" if it isn't wired up).
- **`agents-hub`**: the website that reads published data.

Never write our own http/llm/costs/guards/publish/runner code here — that
all belongs in `agents-core`. If something needed doesn't exist there yet,
don't patch it from this repo; note the gap in STATUS.md under "Needed from
agents-core" and build the smallest local workaround inside `agents/grants/`.

## Commands

```bash
uv sync
uv run pytest                              # unit tests, no live network calls
ruff check .
uv run agents-run grants [--dry-run]       # once agents-core is wired in
uv run python -m evals.grants.run_evals    # eval harness (see evals/grants/)
```

## Conventions

- Cost discipline: no live network calls in tests (fixtures only); SAM.gov is
  never called without `SAM_API_KEY` and the daily budget ledger in
  `agents/grants/state.py` must never be exceeded; every LLM call goes through
  `agents_core.llm` and respects `MAX_RUN_USD`.
- Numbers in narrative text come from data computed in Python, never invented
  by the model — guard every LLM call that reaches published output with
  `agents_core.guards`.
- `content_hash` (in `agents/grants/normalize.py`) covers only the fields
  that should invalidate a cached score/summary. `profile_hash` (in
  `agents/grants/config.py`) covers the whole business profile. Both are part
  of every cache key — see SPEC_GRANTS.md §5.4/§7.3.
- The §6 JSON shapes (`latest.json`, `all.json`) are the website's contract.
  Change them only together with the site, and keep `schemas/grants.schema.json`
  (once `agents_core.export_schemas`-equivalent tooling exists) in sync.
- Keep `DECISIONS.md` and `STATUS.md` current: one-line decisions as they're
  made, and an accurate picture of what's built vs. blocked.

## Where things stand

See `STATUS.md` for the live status (what's implemented, test count, eval
results, blockers, and next steps) and `DECISIONS.md` for the log of
judgment calls made along the way.
