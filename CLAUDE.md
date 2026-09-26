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
  guard, the agent registry/runner. Installed as a git dependency pinned to
  tag `v0.1.0` (commit `b0a292d`, locked in `uv.lock`); read its README for
  the agent contract. This agent is `agents.grants.agent:AGENT`, registered
  under the `agents_core.agents` entry point as `grants`.
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
uv run agents-run grants [--dry-run]       # agent flags: --rescore-all, --lookback-days=N,
                                           #   --sam-request-budget=N
uv run python -m evals.grants.run_evals    # real-LLM evals (~$0.10; see evals/grants/)
uv run python -m tools.record_fixtures grants-gov|sam-from-cache
```

## Conventions

- Cost discipline: no live network calls in tests (fixtures + fakes in
  `tests/grants/fakes.py`; `conftest.py` points agents-core's data/publish/cache
  dirs at tmp so tests never touch the repo's `data/`). SAM.gov is never called
  without `SAM_API_KEY`, and every SAM request goes through
  `fetch_sam.SamBudget`, so agents-core's per-host daily budget and the
  committed ledger in `data/grants/state.json` are both respected — a basic key
  allows ~10 requests/day, so don't burn them on experiments (record fixtures
  from agents-core's HTTP cache with `tools/record_fixtures.py sam-from-cache`).
  Every LLM call goes through `ctx.llm` (agents_core.llm) and respects
  `MAX_RUN_USD`; never import `anthropic` here.
- Numbers in narrative text come from data computed in Python, never invented
  by the model — guard every LLM call that reaches published output with
  `agents_core.guards` (scores: `reasons`/`red_flags`; summaries: every text
  field plus the §7.3 date check), with a deterministic fallback.
- `content_hash` (in `agents/grants/normalize.py`) covers only the fields
  that should invalidate a cached score/summary. `profile_hash` (in
  `agents/grants/config.py`) covers the whole business profile. Both are part
  of every cache key, with the prompt version (`SCORE_PROMPT_VERSION` in
  `scoring.py`, `SUMMARY_PROMPT_VERSION` in `summarize.py`) and model id — see
  SPEC_GRANTS.md §5.4/§7.3. Bump the prompt version whenever a prompt changes,
  then rerun the evals.
- The §6 JSON shapes (`latest.json`, `all.json`) are the website's contract.
  Change them only together with the site. agents-core publishes the JSON
  Schema as `public-data/schema.json` every run; `tests/fixtures/grants/
  schema_snapshot.json` pins it (regenerate it deliberately, see
  `test_schema.py`).
- `data/` is committed run state (CI commits it back); `public-data/` and
  `.cache/` are gitignored (public-data goes to the `data` branch).
- Keep `DECISIONS.md` and `STATUS.md` current: one-line decisions as they're
  made, and an accurate picture of what's built vs. blocked.

## Where things stand

See `STATUS.md` for the live status (what's implemented, test count, eval
results, blockers, and next steps) and `DECISIONS.md` for the log of
judgment calls made along the way.
