# Status

_Final report for the 2026-09-24 overnight autonomous session (~05:47-09:48 UTC)._

## Summary

Worked through every core-independent task from the overnight instructions
(build steps 1-2, 5-8) to completion. Tasks 3 and 4 (the fetchers and LLM
scoring) never started: they depend on `agents-core` exposing an installable
`agents_core` package, which never appeared in the 4-hour window this session
watched for it. **No live Anthropic or SAM spend happened — $0 spent.**

**128 tests passing, `ruff check .` clean**, on `claude/tender-knuth-y8srha`
(all work committed and pushed incrementally; see `git log` for the full
history and `DECISIONS.md` for the reasoning behind each judgment call).

## Done

1. **normalize.py** (§4): SAM + Grants.gov raw JSON → `Opportunity`. SAM
   notice-type mapping, agency/sub_agency/office path splitting, set-aside
   label lookup; Grants.gov applicant-type/value/ALN extraction from
   `search2` + optional `fetchOpportunity` detail. Deadlines with no UTC
   offset are assumed US/Eastern and flagged via `Opportunity.deadline_tz_assumed`
   (§10). `content_hash` covers only the fields that should invalidate a
   cached score/summary. Fixtures in `tests/fixtures/grants/` are hand-built
   to match the documented API shapes (see "Blockers" — live recording
   wasn't possible here).
2. **Dedupe, hard filters, relevance, store** (§5.1-§5.3): `store.py`
   (within-source dedupe by latest `postedDate` + SAM amendment collapsing
   by `solicitation_number`; cross-source dedupe via rapidfuzz
   `token_set_ratio >= 0.9` on normalized title + matching agency;
   `merge_into_store` tracking new/changed ids; `prune_store` dropping
   expired then lowest-relevance overflow). `filters.py` (the 8 ordered hard
   filters with reject-reason codes + a `partition()` helper for the reject
   table). `relevance.py` (the 0-100 deterministic pre-score + candidate
   selection, thresholded and capped).
5. **Output schema (§6) + `--dry-run` (§13)**: `schema.py` (`GrantsLatest`/
   `GrantsAll` pydantic models matching the `latest.json`/`all.json` JSON
   contract field-for-field, `cap_all_rows()` for the row cap and sort
   order). `state.py` (SAM fetch-window/request-count bookkeeping,
   Grants.gov last-run, `profile_hash`, `prompt_versions`, with a
   `profile_changed()` check for §10's rescore-on-profile-edit rule).
   `pipeline.py` (the fetch-free dedupe → hard-filter → relevance core,
   zero LLM calls) + `cli.py` (a **temporary** standalone `--dry-run` that
   runs the pipeline against the eval dataset as demo data and prints the
   reject-reason table plus top-N by relevance — see "For the morning" for
   how this gets replaced once agents-core lands).
6. **Evals (§11)**: `evals/grants/dataset.json` (40 hand-built
   `Opportunity` records: 16 good_fit, 12 maybe, 12 bad_fit, including 2
   explicit hard-blocker cases — clearance-required and 8(a)-sole-source
   ineligibility — and 3 clean/injected description pairs for
   injection-resistance testing). `labels_proposed.json` (my proposed
   labels with a one-line reason each, explicitly marked **PROVISIONAL** —
   not human-reviewed). `run_evals.py` runs the §11 checks; results in
   `evals/results/grants-2026-09-24.json`. **Ranking quality** and
   **injection resistance** run against a deterministic PROXY score (the
   §5.3 relevance pre-score + a regex clearance cap) rather than the real
   LLM rubric — every affected check is tagged `provisional_proxy`,
   `not_applicable`, or `pending_task4` in the output so this is
   unambiguous. Provisional results: precision@10 = 1.0 (pass, ≥0.8), no
   bad_fit in top 5 (pass), hard-blocker handling 100% (pass, both
   explicit cases correctly rejected/capped), injection-resistance pairs
   all Δ=0 (pass, though this only proves the deterministic proxy ignores
   injected text — it says nothing about the real LLM's behavior).
   Stability and summary fidelity are not meaningfully testable yet (no
   real scorer, no summaries built).
7. **`.github/workflows/agent-grants.yml`**: cron `0 13 * * *` (06:00 PT)
   plus `workflow_dispatch` with `rescore_all`/`lookback_days` inputs,
   calling `Kghaffari26/agents-core/.github/workflows/run-agent.yml@main`
   with `agent: grants`, `max_run_usd: 0.50`, `site_repo:
   Kghaffari26/agents-hub`, and `SAM_API_KEY`/`ANTHROPIC_API_KEY` secrets,
   exactly as instructed. **This will not pass GitHub Actions validation
   yet** — see "Needed from agents-core" below; the gap is documented
   in-file and here rather than worked around by modifying agents-core.
8. **README.md / CLAUDE.md**: describe the multi-repo architecture (this
   repo holds only the grants agent; `agents-core` provides shared
   http/llm/costs/guards/publish/runner; `agents-hub` is the site), what's
   implemented, and repo conventions.

## Needed from agents-core

Checked `Kghaffari26/agents-core` roughly every 15 minutes from ~05:47 UTC
to the 4-hour cutoff at ~09:48 UTC. It never changed: `main` stayed at
`f79b6aa`, and no other branch was ever created. Specifically:

- **No `src/agents_core/` package.** The repo is still monorepo-shaped:
  `core/` (not `src/agents_core/`), `pyproject.toml` name is `agents-hub`,
  `[tool.uv] package = false` (not built as an installable wheel at all).
  `core/guards.py` and `core/registry.py` exist, but not at the
  `src/agents_core/guards.py` / `src/agents_core/registry.py` paths this
  session was told to look for. **This blocked essentially everything
  multi-repo-shaped**: installing it as a git dependency, importing
  `agents_core.llm`/`agents_core.http`/`agents_core.guards`, and
  registering `grants` through an `agents_core.agents` entry point.
- **No `agents_core.agents` entry-point group.** `core/registry.py`
  currently uses a plain `AGENT_IDS` list + `agents/<id>/agent.py:AGENT`
  convention (monorepo-style), not a pip entry-point group a separate repo
  could register against.
- **`run-agent.yml` doesn't support a multi-repo caller.** It only declares
  `agent`/`args` `workflow_call` inputs (no `max_run_usd`, no `site_repo`),
  checks out and commits straight to the *calling* repo's own `main`
  (assuming that repo *is* the monorepo with `core/` and `site/` in it),
  and runs the hardcoded `python -m core.runner <agent>` rather than an
  installed package's entry point. There's also no documented "public-data
  data-branch contract" anywhere in agents-core for a separate repo like
  this one to publish against.

None of this was modified in `agents-core` (per instructions). Where a gap
blocked a task outright (fetchers, scoring), that task simply didn't start.
Where a minimal local workaround was possible, it's built inside
`agents/grants/` and flagged as temporary (`pipeline.py`/`cli.py`'s
standalone `--dry-run`; `evals/grants/run_evals.py`'s proxy scorer).

## Not started (blocked on agents-core)

- **Task 3**: `fetch_sam.py` (budgeted SAM window fetch, pagination,
  description-fetch accounting, 404-as-empty) and `fetch_grants_gov.py`
  (per-keyword `search2` + `fetchOpportunity` with caching) — need
  `agents_core.http`'s budgeted/cached request client.
- **Task 4**: `scoring.py` (LLM rubric via Batch API, caching by
  `content_hash`+`profile_hash`+prompt version, caps, recommendation
  bands) and `summarize.py` (top-20 summaries with guard + template
  fallback) — need `agents_core.llm` and `agents_core.guards`.
- Re-running `evals/grants/run_evals.py` against the *real* rubric scorer
  once task 4 exists, and actually exercising stability and summary
  fidelity (currently `not_applicable`/`pending_task4`).
- Wiring `agents/grants/` into `agents_core`'s `Agent` base class and
  registry so `uv run agents-run grants [--dry-run]` works for real,
  replacing `cli.py`'s temporary standalone dry-run.

## Environment / cost notes

- No `ANTHROPIC_API_KEY`, `SAM_API_KEY`, or `MAX_RUN_USD` in this
  environment all night. **No live Anthropic spend happened or was
  possible — $0 spent.** No SAM.gov calls were made (correctly — no key
  present, and the spec requires never calling SAM without one).
- `api.grants.gov` is blocked by this environment's egress policy (the
  proxy 403s the CONNECT). The instruction to "use Grants.gov live to
  record fixtures" couldn't be carried out here; `tests/fixtures/grants/`
  and `evals/grants/dataset.json` are hand-built to match the documented
  response shapes instead. Worth a real recording pass from an environment
  with access.
- `agents-core` pinned SHA: **none** — never became installable tonight.

## For the morning

1. Read `DECISIONS.md` (full chronological log of every judgment call) and
   this file.
2. Check whether `Kghaffari26/agents-core` now has an installable
   `src/agents_core/` package. If someone finishes it (or extends
   `run-agent.yml` per "Needed from agents-core" above), pick a commit SHA
   and:
   ```
   cd /home/user/sam-agent
   uv add "agents-core @ git+https://github.com/Kghaffari26/agents-core@<sha>"
   ```
   Then build task 3 (fetchers) and task 4 (scoring/summaries), replace
   `agents/grants/cli.py`'s temporary standalone `--dry-run` with a real
   `AGENT = ...` registered as `grants`, and re-run
   `evals/grants/run_evals.py` against the real scorer (delete or clearly
   relabel the proxy-scorer path in that file once it's no longer needed).
3. If `agents-core` is still monorepo-shaped: it needs either (a) a
   `src/agents_core/` package extracted from `core/` with a real
   `pyproject.toml`/entry points, or (b) `run-agent.yml` extended with
   `max_run_usd`/`site_repo` inputs and a `public-data/` publish contract
   documented somewhere. This repo is otherwise ready to consume whichever
   lands first.
4. `evals/grants/labels_proposed.json` needs a human pass — it's my
   overnight guess at `good_fit`/`maybe`/`bad_fit` for the 40 items, not
   reviewed by you. The dataset itself (`dataset.json`) is hand-built, not
   pulled from real listings — a live Grants.gov recording pass (from
   network access that isn't blocked) would improve realism.
5. Try `uv run python -m agents.grants.cli --dry-run` to see the current
   pipeline work end-to-end against demo data (zero LLM calls, ~instant).
