# sam-agent

The **Grants & Contracts Finder** agent: finds federal contract opportunities
(SAM.gov) and grant opportunities (Grants.gov) that fit a configured business
profile, screens them with deterministic rules, scores the survivors with an
LLM rubric, writes bid/no-bid summaries for the top 20, and publishes a
ranked, explainable shortlist for a website.

Full spec: [`docs/specs/SPEC_GRANTS.md`](docs/specs/SPEC_GRANTS.md). See
[`CLAUDE.md`](CLAUDE.md) for repo conventions and [`STATUS.md`](STATUS.md) /
[`DECISIONS.md`](DECISIONS.md) for what's built and why.

## Architecture (multi-repo)

This repo holds **only** the grants agent. Shared code — HTTP with retries,
per-host rate limits, daily request budgets and an on-disk cache; the LLM
client (tiers, Batch API, structured outputs, the per-run `MAX_RUN_USD` cap);
cost tracking; the number guard; publishing; the runner — comes from
[`agents-core`](https://github.com/Kghaffari26/agents-core), installed as a git
dependency pinned to tag `v0.1.0` (commit `b0a292d`, see `uv.lock`).

The agent registers under agents-core's `agents_core.agents` entry-point group
as `grants` (`agents.grants.agent:AGENT`), so agents-core's `agents-run`
command runs it. Output follows agents-core's data-branch contract under
`public-data/`; the scheduled workflow force-pushes that to this repo's `data`
branch and notifies the site, [`agents-hub`](https://github.com/Kghaffari26/agents-hub).

```
fetch     SAM.gov (one budgeted window/run) + Grants.gov (search2 per keyword,
          fetchOpportunity for new/changed ids, cached permanently)
transform normalize -> dedupe -> merge into store -> hard filters -> relevance
analyze   rubric scoring (fast tier, Batch API, cached) -> top 20 ->
          budgeted SAM description fetches (+ rescore on change) ->
          summaries (smart tier, cached, number/date guard, template fallback)
publish   public-data/latest.json, all.json, history/, manifest-entry.json,
          costs-summary.json, schema.json   (agents-core writes these)
```

```
sam-agent/
├── config/
│   ├── business_profile.toml   # who we match for: NAICS, keywords, set-asides, ...
│   └── grants.toml             # run settings, SAM/Grants.gov knobs (§8)
├── agents/grants/
│   ├── agent.py                 # GrantsAgent (agents_core Agent) + AGENT entry point
│   ├── fetch_sam.py             # budgeted SAM window, pagination, descriptions
│   ├── fetch_grants_gov.py      # search2 per keyword, fetchOpportunity + detail cache
│   ├── normalize.py             # SAM/Grants.gov raw JSON -> Opportunity
│   ├── store.py                 # dedupe, merge, prune, store.json.gz
│   ├── filters.py               # deterministic hard filters (§5.2)
│   ├── relevance.py             # deterministic 0-100 pre-score (§5.3)
│   ├── prompting.py             # prompt payloads, eligibility facts, guard facts, dates
│   ├── scoring.py               # LLM rubric, batch, cache keys, caps, bands (§5.4)
│   ├── summarize.py             # top-20 summaries, guard, cache (§7.3/§7.4)
│   ├── templates.py             # template fallback summary + headline
│   ├── output.py                # builds the §6 latest.json body and all.json
│   ├── schema.py                # §6 output models (the site contract)
│   ├── state.py                 # data/grants/state.json (SAM window + ledger, ...)
│   ├── config.py / models.py / setasides.py / eligibility.py
├── data/                        # committed run state (written by runs, committed by CI)
│   ├── costs.jsonl              # agents-core cost log (every LLM call + run line)
│   └── grants/                  # state.json, store.json.gz, grants_gov_details.json.gz
├── tests/grants/                # unit + end-to-end tests, all mocked (no network)
├── tests/fixtures/grants/       # live-recorded + hand-built API fixtures
├── evals/grants/                # labeled 40-item eval set + harness (§11)
├── tools/record_fixtures.py     # re-record fixtures (Grants.gov live; SAM from cache)
└── .github/workflows/agent-grants.yml
```

## Running it

Python 3.12, managed with [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
uv run agents-run grants --dry-run   # fetch + filter + pre-score; prints the reject
                                     # table and top 20 by relevance; no LLM, no publish
uv run agents-run grants             # full run: scores, summarizes, publishes public-data/
```

Agent flags (forwarded by agents-core): `--rescore-all` (ignore the score and
summary caches), `--lookback-days=N` (override the SAM window, for backfills),
`--sam-request-budget=N` (lower today's SAM request cap below
`sam_daily_request_budget`).

Environment: `SAM_API_KEY` (SAM is skipped without it, never called keyless),
`ANTHROPIC_API_KEY` or `AGENTS_ANTHROPIC_API_KEY` (read by agents-core),
`AGENTS_CORE_MAX_RUN_USD` (default `0.50`). Grants.gov needs no key.

**SAM.gov quota:** a basic key allows about 10 requests/day. Each run fetches
one window (1 request per 1,000 notices) plus up to
`max_sam_description_fetches` descriptions per day, and never exceeds
`sam_daily_request_budget` (8): agents-core's per-host daily budget and the
committed ledger in `data/grants/state.json` both enforce it.

## Development

```bash
uv run pytest                              # all mocked/fixture-based, no network
uv run ruff check .
uv run python -m evals.grants.run_evals    # real-LLM evals (~$0.10), see evals/grants/
uv run python -m tools.record_fixtures grants-gov      # re-record Grants.gov fixtures
uv run python -m tools.record_fixtures sam-from-cache  # SAM fixture from the HTTP cache
```

`schema.json` (the JSON Schema of `latest.json`) is written by agents-core on
every run; `tests/grants/test_schema.py` snapshots it so a contract change is
always deliberate.
