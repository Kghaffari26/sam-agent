# Status

_Last updated: 2026-09-24 ~05:55 UTC, autonomous overnight session in progress._

This is a checkpoint, not the final report — the session is still running
(waiting on `agents-core`, checking back every ~15 min). It will be rewritten
once the run ends (either `agents-core` lands and tasks 3-4 complete, or ~4
hours elapse with nothing found).

## Done so far

All core-independent work from the task list is complete, committed, and
pushed to `claude/tender-knuth-y8srha`:

1. **normalize.py** (§4): SAM + Grants.gov → `Opportunity`, with
   hand-built fixtures in `tests/fixtures/grants/`, deadline timezone
   handling (`deadline_tz_assumed`), and `content_hash`.
2. **Dedupe, hard filters, relevance, store** (§5.1-§5.3): `store.py`
   (within/cross-source dedupe via rapidfuzz, merge/prune/new/changed),
   `filters.py` (8 ordered hard filters + reject-reason table),
   `relevance.py` (0-100 pre-score + candidate selection).
5. **Output schema** (§6) + **--dry-run** (§13): `schema.py`
   (`GrantsLatest`/`GrantsAll` pydantic models matching the JSON contract),
   `state.py` (fetch window / profile-hash / prompt-version bookkeeping),
   `pipeline.py` + `cli.py` (a temporary standalone `--dry-run` that fetches
   demo data, dedupes, hard-filters, and pre-scores with **zero LLM calls**).
6. **Evals** (§11): `evals/grants/dataset.json` (40 hand-built items) and
   `labels_proposed.json` (my proposed labels, marked PROVISIONAL), plus
   `run_evals.py`. Ranking-quality and injection-resistance currently run
   against a **deterministic proxy score** (relevance pre-score + a regex
   clearance cap), not the real LLM rubric — see "Blockers" below. Results
   in `evals/results/grants-2026-09-24.json`.
7. **`.github/workflows/agent-grants.yml`**: cron + `workflow_dispatch`,
   calling `Kghaffari26/agents-core`'s reusable `run-agent.yml` with the
   inputs specified — **will not validate yet**, see "Needed from
   agents-core" below.
8. (partial) README.md and CLAUDE.md updated to describe the multi-repo
   architecture and current state.

**Test count: 128 passing, `ruff check .` clean.**

## Needed from agents-core

As of `Kghaffari26/agents-core` @ `f79b6aa` (`main`, only branch):

- No commit yet has both `src/agents_core/guards.py` and
  `src/agents_core/registry.py` — the repo is still monorepo-shaped
  (`core/…`, `pyproject.toml` name `agents-hub`, `[tool.uv] package =
  false`), not an installable `agents_core` package. A background poller
  (`/tmp/agents_core_poll.log`, checks every ~15 min for up to 4h from
  ~05:47 UTC) is watching for this; nothing found as of this checkpoint.
- Its `.github/workflows/run-agent.yml` only declares `agent`/`args`
  `workflow_call` inputs and runs the old monorepo `python -m core.runner`,
  checking out and committing straight to the *calling* repo's own `main`.
  It does not accept `max_run_usd` or `site_repo`, and there's no documented
  "public-data data-branch contract" for a multi-repo caller to follow. Our
  `agent-grants.yml` is written to the target contract per instructions, but
  won't pass GitHub Actions validation until agents-core's reusable workflow
  is extended to match (declare those inputs; run this repo's `agents-run`
  entry point via the installed `agents_core` package; publish to
  `public-data/` per whatever that contract turns out to be).
- No `agents_core.agents` entry-point group to register `grants` against yet
  (current registry.py uses a plain `AGENT_IDS` list + `agents/<id>/agent.py`
  convention).

None of the above was modified in `agents-core` — per instructions, gaps are
listed here and worked around minimally inside `agents/grants/` (see
`pipeline.py`/`cli.py`'s temporary standalone `--dry-run`).

## Not started (blocked on agents-core)

- **Task 3**: `fetch_sam.py`, `fetch_grants_gov.py` (need `agents_core.http`'s
  budget/cache/retry client).
- **Task 4**: LLM rubric scoring (`scoring.py`, needs `agents_core.llm`'s
  Batch API client) and top-20 summaries (`summarize.py`, needs
  `agents_core.llm` + `agents_core.guards`).
- Re-running the evals (§11) against the *real* rubric scorer once task 4
  exists — the current run is a clearly-labeled proxy.

## Environment / cost notes

- No `ANTHROPIC_API_KEY`, `SAM_API_KEY`, or `MAX_RUN_USD` in this
  environment. No live Anthropic spend has happened or is possible tonight;
  **$0 spent**.
- SAM.gov has not been and will not be called live (no key). All SAM
  handling is built and tested against hand-built fixtures matching the
  documented v2 response shape.
- `api.grants.gov` is blocked by this environment's egress policy (proxy
  403s the CONNECT), so the "use Grants.gov live to record fixtures"
  instruction couldn't be carried out — fixtures are hand-built instead.
  Worth a real recording pass from an environment with access.

## For the morning

1. Read `DECISIONS.md` (chronological log of every judgment call made
   tonight) and this file.
2. Check whether `agents-core` now has the expected `src/agents_core/`
   layout. If the background poll already found a SHA, it's recorded in
   `/tmp/agents_core_found_sha.txt` (that's a session-local temp file, not
   committed — re-derive it from DECISIONS.md/this file's final version if
   the container has been recycled).
3. If `agents-core` is ready: `uv add "agents-core @
   git+https://github.com/Kghaffari26/agents-core@<sha>"`, then build task 3
   (fetchers) and task 4 (scoring/summaries), wire `agents/grants/cli.py`'s
   logic into a real `AGENT = ...` registered under `grants`, and re-run
   `evals/grants/run_evals.py` against the real scorer.
4. If `agents-core` is still not ready: someone needs to either finish its
   `src/agents_core/` package (with `guards.py`, `registry.py`, an `llm.py`
   with a Batch API client, an `http.py` with budgeted requests, `publish.py`
   for the `public-data/` contract) or extend `run-agent.yml`'s inputs — this
   repo is otherwise ready to consume it the moment either exists.
5. `evals/grants/labels_proposed.json` needs a human pass — it's my
   overnight guess at good_fit/maybe/bad_fit, not reviewed by you.
