# sam-agent

The **Grants & Contracts Finder** agent: finds federal contract opportunities
(SAM.gov) and grant opportunities (Grants.gov) that fit a configured business
profile, screens them with deterministic rules, scores the survivors with an
LLM rubric, writes bid/no-bid summaries for the top 20, researches the best
matches with a budgeted tool-using agent loop, and publishes a ranked,
explainable shortlist for a website.

## Highlights

- **Agent loop, not a prompt chain.** For each new *Pursue* match (at most 3
  per run), a bid-research loop built on `agents_core.agent_loop` reads the notice,
  up to 3 SAM attachments (PDF/DOCX text extraction), USAspending.gov prior
  awards and Grants.gov detail, then calls `finish` with a brief covering what
  they're buying, evaluation criteria, the likely incumbent and prior award
  values, risks and a go/no-go. Budgets per opportunity: 10 steps, $0.12, 240 s.
  Attachment downloads count against the SAM request ledger (at most 4 per
  run). Tool outputs are wrapped as untrusted data. Results are cached per
  notice version (content hash). See [`agents/grants/research.py`](agents/grants/research.py)
  and [SPEC §6.3](docs/specs/SPEC_GRANTS.md#63-bid-research-additive-schema_version-110).
- **Guards: numbers come from data.** Every model-written field that reaches
  the site (score reasons and red flags, summaries, research briefs) is
  checked by the agents-core number guard against the numbers in its input,
  plus a date check. The research guard also rejects award ids USAspending
  never returned. Prior-award dollar values are copied from USAspending in code,
  never from the model. Failures are retried once, then replaced by a
  deterministic template labelled `narrative_source: "template"`.
- **Evals with history and a PR gate.** Three suites on `agents_core.evals`
  ([`evals/grants/suites.py`](evals/grants/suites.py)) append to
  [`evals/history.jsonl`](evals/history.jsonl); `.github/workflows/evals.yml`
  runs them on PRs through agents-core's `run-evals.yml` (spend cap $1.00) and
  fails on regressions. Latest scores (2026-09-27): see [Evals](#evals).
- **Tracing.** Every run publishes a redacted `trace.json` to the `data`
  branch: spans for each phase, each analyze step, every LLM call, HTTP
  request, guard check, agent loop and tool call, with tokens, cost and
  latency. `manifest-entry.json` carries a `trace_summary`.
- **Costs.** Each run is capped by `MAX_RUN_USD` ($0.50), checked before
  every call. Scoring runs on the Batch API at half price, and scores,
  summaries and research are cached, so an unchanged day costs $0. The
  2026-09-27 live run cost **$0.0043** (two rescored items). A full eval run
  costs about $0.27 (scoring $0.055, summaries $0.088,
  five research loops $0.123 — $0.019 to $0.034 each, 3 steps). SAM.gov requests stay within a ~10/day key:
  budgeted, ledgered, never retried.
- **Degrades instead of failing.** With no Anthropic key the run still
  publishes (status ok): cached scores and research, template summaries, and
  a `meta.warnings` entry. A rejected or expiring SAM key, or an exhausted SAM
  or Anthropic budget, also opens an `ops-alert` GitHub issue (at most weekly
  per title).

Full spec: [`docs/specs/SPEC_GRANTS.md`](docs/specs/SPEC_GRANTS.md). See
[`CLAUDE.md`](CLAUDE.md) for repo conventions, [`STATUS.md`](STATUS.md) /
[`DECISIONS.md`](DECISIONS.md) for what's built and why, and
[`docs/case-studies.md`](docs/case-studies.md) for five real incidents (how
each was caught and fixed).

## Demo

No keys needed. The research loop runs against the fixture world; this is the
live eval case `r1-va-dashboard`, replayed from its recorded trajectory:

```bash
uv sync
uv run pytest tests/grants/test_research_replay.py -q   # replays a recorded loop offline
uv run python -m evals.grants.demo                      # prints the brief it produced
```

Output (abridged):

```
r1-va-dashboard: stop=finished steps=3 guard=llm
  step 1: get_opportunity(id='sam:r1va…')
  step 1: list_attachments(id='sam:r1va…')
  step 2: read_attachment(id='sam:r1va…', name='attachment_1')
  step 2: read_attachment(id='sam:r1va…', name='attachment_2')
  step 2: usaspending_prior_awards(agency='VETERANS AFFAIRS, DEPARTMENT OF', naics='541512',
                                   keywords=['dashboard', 'benefits', 'modernization'])

What they're buying: VA/VBA wants to re-platform a legacy ASP.NET benefits-status dashboard
  into a React front end with Python (FastAPI) services on AWS GovCloud, …
Evaluation criteria: Factor 1: Technical Approach; Factor 2: Past Performance; Factor 3: Price …
Likely incumbent: ACME DIGITAL SERVICES LLC -- holds award 36C10B21C0045 ($3,412,500) for
  "VBA Benefits Status Dashboard Sustainment and Enhancements", a strong match …
  prior award 36C10B21C0045: ACME DIGITAL SERVICES LLC, $3,412,500.00 (https://www.usaspending.gov/award/…)
  prior award 36C10B23F0188: BLUE HARBOR TECHNOLOGIES INC, $1,250,000.00 (https://www.usaspending.gov/award/…)
Go/no-go: GO -- excellent technical fit, small-business set-aside at a target agency, but an
  entrenched incumbent, so win probability hinges on the technical/past-performance narrative.
```

The fixture world (5 opportunities with real PDF/DOCX attachments, a prompt
injection and a Top Secret requirement among them) is in
[`evals/grants/research/`](evals/grants/research/). USAspending.gov and sam.gov
downloads weren't reachable from the environment that built this, so those
responses are hand-built to the documented shapes (see STATUS.md).

With keys (`SAM_API_KEY`, `ANTHROPIC_API_KEY` or `AGENTS_ANTHROPIC_API_KEY`):

```bash
uv run agents-run grants --dry-run --sam-request-budget=1   # 1 SAM request, no LLM
uv run agents-run grants --sam-request-budget=3             # full run; public-data/latest.json
AGENTS_CORE_EVAL_MAX_USD=1.00 uv run agents-evals run evals.grants.suites:RESEARCH
```

## Architecture (multi-repo)

This repo holds **only** the grants agent. Shared code — HTTP with retries,
per-host rate limits, daily request budgets and an on-disk cache; the LLM
client (tiers, Batch API, structured outputs, the per-run `MAX_RUN_USD` cap);
cost tracking; the number guard; publishing; the runner — comes from
[`agents-core`](https://github.com/Kghaffari26/agents-core), installed as a git
dependency pinned to tag `v0.3.0` (commit `bcfb9c5`, see `uv.lock`), plus
its agent loop, tracing, evals and ops alerts.

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
          summaries (smart tier, cached, number/date guard, template fallback) ->
          bid research for up to 3 new Pursue matches (agent loop, cached, guarded)
publish   public-data/latest.json, all.json, history/, manifest-entry.json,
          costs-summary.json, schema.json, trace.json   (agents-core writes these)
```

```
sam-agent/
├── config/
│   ├── business_profile.toml   # who we match for: NAICS, keywords, set-asides, ...
│   ├── grants.toml             # run settings, SAM/Grants.gov/research knobs (§8)
│   └── models.toml             # agents-core tier overrides (fast tier temperature 0)
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
│   ├── research.py              # bid-research agent loop: tools, guard, fallback (§6.3)
│   ├── attachments.py           # SAM attachments: naming, PDF/DOCX text extraction
│   ├── usaspending.py           # USAspending.gov prior-award search
│   ├── llm_compat.py            # local shim: per-tier temperature on sync calls
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
├── evals/grants/                # suites.py (agents_core.evals), 40-item labeled set,
│                                #   research/ fixture world, trajectories/
├── evals/history.jsonl          # eval score history (the PR gate's baseline)
├── tools/                       # record_fixtures.py, make_research_fixtures.py
└── .github/workflows/           # agent-grants.yml (daily), evals.yml (PRs)
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
AGENTS_CORE_EVAL_MAX_USD=1.00 uv run agents-evals run evals.grants.suites:SCORING \
  evals.grants.suites:SUMMARIES evals.grants.suites:RESEARCH   # real LLM calls
uv run agents-evals compare                # latest vs previous history entry
uv run python -m tools.record_fixtures grants-gov      # re-record Grants.gov fixtures
uv run python -m tools.record_fixtures sam-from-cache  # SAM fixture from the HTTP cache
```

`schema.json` (the JSON Schema of `latest.json`) is written by agents-core on
every run; `tests/grants/test_schema.py` snapshots it so a contract change is
always deliberate.

## Evals

Latest run: 2026-09-27, models `claude-haiku-4-5-20251001` (scoring, judges) and
`claude-sonnet-5` (summaries, research). History: [`evals/history.jsonl`](evals/history.jsonl);
full per-case results: [`evals/results/2026-09-27.json`](evals/results/2026-09-27.json).

| Suite | Cases | Pass rate | Scores | Cost |
|---|---|---|---|---|
| `grants-scoring` (40 labeled items, scored twice) | 1 | 1.00 | precision@10 1.00 · no bad fit in top 5 1.00 · hard blockers 1.00 · **same recommendation across 2 samples 0.967** (target ≥ 0.90) · within ±5 1.00 · injection resistance 1.00 | $0.055 |
| `grants-summaries` | 5 | 1.00 | guard + dates 1.00 · next steps 1.00 · LLM-written 1.00 · judge 0.85 | $0.088 |
| `grants-research` (agent loop) | 5 | 0.80 | required tools 1.00 · forbidden tools 1.00 · ≤ 10 steps 1.00 · stop = finished 1.00 · go/no-go 0.80 · guard passed 1.00 · content checks 1.00 · ≤ 3 attachments 1.00 · cites USAspending 1.00 · judge 0.95 | $0.123 |

Labels are provisional (proposed by Claude, not human-reviewed). The one
research miss is `r3-nsf-grant`: labelled `go`, but the model called `no_go`
with a defensible rationale (a competitive research grant, and the firm has no NSF
track record). The judge scored that brief 1.0; the label needs a human call.
The LLM judges aren't calibrated against human labels yet.
