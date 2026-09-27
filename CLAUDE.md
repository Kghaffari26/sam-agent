# sam-agent

The grants/contracts finder agent, one repo in a multi-repo split (see
`docs/specs/SPEC_GRANTS.md`, written for a monorepo — where it and this file
disagree on repo layout, this file wins; its §2-§13 content, especially the
§6 JSON output contract, still governs).

## Repo split

- **This repo (`sam-agent`)**: the `grants` agent only — `agents/grants/`,
  `config/`, its tests and evals.
- **`agents-core`**: shared code (`agents_core` package) — LLM batch client,
  HTTP with request budgets and conditional downloads, cost tracking, publish
  helpers, the number guard, the agent loop (`agent_loop`), tracing, evals,
  ops alerts, the agent registry/runner. Installed as a git dependency pinned
  to tag `v0.3.1` (commit `dba5e86`, locked in `uv.lock`); read its README and
  CHANGELOG for the agent contract. This agent is `agents.grants.agent:AGENT`,
  registered under the `agents_core.agents` entry point as `grants`.
- **`agents-hub`**: the website that reads published data.

Never write our own http/llm/costs/guards/publish/runner/loop/tracing/evals code
here — that all belongs in `agents-core`. If something needed doesn't exist
there yet (or is broken there), don't patch it from this repo; note the gap in
STATUS.md under "Needed from agents-core" and build the smallest local
workaround inside `agents/grants/` (today: none).

## Commands

```bash
uv sync
uv run pytest                              # unit tests, no live network calls
ruff check .
uv run agents-run grants [--dry-run]       # agent flags: --rescore-all, --lookback-days=N,
                                           #   --sam-request-budget=N
uv run agents-evals run --total-max-usd 1.00 evals.grants.suites:SCORING \
  evals.grants.suites:SUMMARIES evals.grants.suites:RESEARCH   # real LLM calls, see evals/grants/
uv run agents-evals compare                # latest vs previous entry of evals/history.jsonl
uv run python -m tools.record_fixtures grants-gov|sam-from-cache
uv run python -m tools.make_research_fixtures   # rebuild the research eval PDF/DOCX files
```

## Conventions

- Cost discipline: no live network calls in tests (fixtures + fakes in
  `tests/grants/fakes.py`; `conftest.py` points agents-core's data/publish/cache/
  evals dirs at tmp and unsets real keys and `GITHUB_TOKEN`, so tests never touch
  the repo's `data/`, spend money or open issues). SAM.gov is never called
  without `SAM_API_KEY`, and every SAM request — searches, descriptions and
  research attachment downloads — goes through `fetch_sam.SamBudget`, so
  agents-core's per-host daily budget and the committed ledger in
  `data/grants/state.json` are both respected — a basic key allows ~10
  requests/day, so don't burn them on experiments (record fixtures from
  agents-core's HTTP cache with `tools/record_fixtures.py sam-from-cache`).
  Every LLM call goes through `ctx.llm` (agents_core.llm) and respects
  `MAX_RUN_USD`; never import `anthropic` here.
- Numbers in narrative text come from data computed in Python, never invented
  by the model — guard every LLM call that reaches published output with
  `agents_core.guards` (scores: `reasons`/`red_flags`; summaries: every text
  field plus the §7.3 date check; bid research: every narrative field against
  the tool outputs, the date check and the cited award ids), with a
  deterministic fallback. Prior-award values in research are copied from
  USAspending in code, never taken from the model.
- Bid research (`research.py`, SPEC §6.3) is an `agents_core.agent_loop` with
  per-opportunity budgets (10 steps, $0.12, from `[research]` in
  `config/grants.toml`). Tool outputs are untrusted data. Its output is
  additive (`top_matches[i].research`, `all.json` `has_research`); keep it so.
- Non-fatal problems go to `meta.warnings` via `ctx.warn`; things a human must
  fix (SAM key rejected/expiring, SAM or Anthropic budget exhausted) also get
  `ctx.alert` (one `ops-alert` issue per title, at most weekly). A run without
  an Anthropic key must still publish (status ok, cached/template output, a
  warning).
- Sampling: the fast tier (scoring) runs at `temperature = 0` from
  `config/models.toml`; the LLM judges pass `temperature=0` to `LLMJudge`.
  agents-core (>= v0.3.1) sends it in `extra_body`, so fakes that check it read
  `kwargs["extra_body"]["temperature"]`. Eval spend is one total cap
  (`--total-max-usd` / `run-evals.yml` `total_max_usd`).
- `content_hash` (in `agents/grants/normalize.py`) covers only the fields
  that should invalidate a cached score/summary/research. `profile_hash` (in
  `agents/grants/config.py`) covers the whole business profile. Both are part
  of every cache key, with the prompt version (`SCORE_PROMPT_VERSION` in
  `scoring.py`, `SUMMARY_PROMPT_VERSION` in `summarize.py`,
  `RESEARCH_PROMPT_VERSION` in `research.py`) and model id — see SPEC_GRANTS.md
  §5.4/§7.3/§6.3. Bump the prompt version whenever a prompt changes, then rerun
  the evals and commit `evals/history.jsonl` (the PR gate in
  `.github/workflows/evals.yml` compares against it).
- The §6 JSON shapes (`latest.json`, `all.json`) are the website's contract.
  Change them only together with the site; additive fields get their own
  SPEC §6.x section. agents-core publishes the JSON Schema as
  `public-data/schema.json` every run; `tests/fixtures/grants/
  schema_snapshot.json` pins it (regenerate it deliberately, see
  `test_schema.py`).
- `data/` is committed run state (CI commits it back); `public-data/` and
  `.cache/` are gitignored (public-data goes to the `data` branch, which
  run-agent.yml restores before each run; `trace.json` is published there).
- The reusable workflows declare no permissions: each calling job grants its
  own (agent job: `contents: write` + `issues: write`; evals job:
  `contents: read`).
- Keep `DECISIONS.md` and `STATUS.md` current: one-line decisions as they're
  made, and an accurate picture of what's built vs. blocked.

## Where things stand

See `STATUS.md` for the live status (what's implemented, test count, eval
results, blockers, and next steps), `DECISIONS.md` for the log of judgment
calls made along the way, and `docs/case-studies.md` for incidents and fixes.
