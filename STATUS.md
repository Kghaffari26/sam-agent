# Status

_Updated 2026-09-27 (session: agents-core v0.3.1 upgrade; before that: v0.3.0, bid research, tracing, evals)._

## Summary

The agent runs on **agents-core v0.3.1** (tag `v0.3.1` → `dba5e86`, locked in
`uv.lock`; the v0.3.0 notes below are from the previous session) with every v0.1.0 workaround that v0.2.0 made unnecessary removed. New
this session: a bid-research agent loop (SPEC §6.3, additive output), run
tracing (`trace.json`), evals ported to `agents_core.evals` with a history and a
PR gate, warnings and ops alerts, and a no-Anthropic-key fallback.

- **Tests: 244 passing; `ruff check .` clean; actionlint clean** on both
  workflows. No network in tests.
- **Anthropic spend this session: $0.3331** (cap $1.50) = evals $0.3289
  (`data/eval_costs.jsonl`: one $0.03 research smoke case, the full run $0.2104,
  a summaries re-run $0.0884) + the live agent run $0.0043 (`data/costs.jsonl`),
  plus two probe calls under $0.001 (not logged: checking whether the API
  accepts `temperature`).
- **SAM.gov requests this session: 3 of 3 allowed** (ledger
  `data/grants/state.json` → `sam.requests["2026-09-27"] = 3`): 1 window search
  (the dry run; the real run reused it from the HTTP cache) + 2 description
  fetches.

## agents-core v0.3.1 upgrade (2026-09-27)

Pin and both workflow refs → `v0.3.1`. `llm_compat.py` and the `suites.Judge`
subclass deleted (v0.3.1 sends `temperature` in `extra_body`); judges use
`LLMJudge(temperature=0)`; the per-suite eval shares replaced by one total cap
(`evals.yml` `total_max_usd: "1.00"`). Tests 244 passing, ruff clean.
**Stability re-run** (`grants-scoring` only, 2 Batch API samples, total cap $0.30,
no SAM requests): **same recommendation 0.967** (29/30; ≥ 0.90 ✅), within ±5 1.00,
all other scoring checks 1.00, **$0.055**. The one flip is `gg:g007` (Consider 74 ↔
Pursue 75) on the band edge. Same result as the v0.3.0 run: scoring goes through
batches, where temperature 0 already applied, so the fix changes the sync paths
(rescores, guard retries, judges), not this number.

## Live run (2026-09-27, `--sam-request-budget=3`, `AGENTS_CORE_MAX_RUN_USD=0.40`)

| Run | What happened | LLM | Cost | SAM |
|---|---|---|---|---|
| dry run | window 09/25–09/27: 421 notices; Grants.gov 354 hits, 49 details; 438 active, 8 at/above relevance | 0 | $0 | 1 |
| real run | 8 scores cached; 2 SAM descriptions fetched → 2 sync rescores (through the temperature shim); 6 matches, top 8; **0 Pursue, so no research**; status ok, `warnings: []`, `trace.json` + `trace_summary` published | 2 | **$0.0043** | 2 |

The real run exposed a bug (fixed in `cec2caf`, with a regression test): a
rescored item kept publishing the summary cached for its old content hash. See
`docs/case-studies.md` #5. The two affected summaries regenerate on the next run.

## Evals (`evals/history.jsonl`, `evals/results/2026-09-27.json`)

| Suite | Pass rate | Key scores | Cost |
|---|---|---|---|
| `grants-scoring` | 1.00 | precision@10 1.00, no bad fit in top 5, hard blockers 1.00, **same recommendation 0.967** (≥ 0.90 ✅; was 0.867/0.967 before temperature 0), within ±5 1.00, injection 1.00 | $0.055 |
| `grants-summaries` | 1.00 (after the judge rubric fix; 0.40 before) | guard+dates 1.00, next steps 1.00, LLM-written 1.00, judge 0.85 | $0.088 |
| `grants-research` | 0.80 | required/forbidden tools 1.00, ≤ 10 steps 1.00 (3 steps each), finished 1.00, go/no-go 0.80, guard 1.00, content 1.00, ≤ 3 attachments 1.00, USAspending citations 1.00, judge 0.95 | $0.123 |

- The summary judge first scored 0.55: it penalized dollar amounts copied from
  the input, which the rubric allows. The rubric now says so; the re-run scored 0.85.
- Research miss: `r3-nsf-grant` is labelled `go`; the model said `no_go`
  (competitive research grant, no NSF track record). The judge scored that
  brief 1.0. **Needs a human decision on the label.**
- Labels are still PROVISIONAL (Claude's proposals). Judges are not calibrated
  against human scores (`LLMJudge.calibrate`).
- Number guard limit seen in the research demo: "3-year total period" (derived
  from 3 × 12 months) passes because a 3 appears elsewhere in the input. The
  guard checks that numbers exist in the data, not that they're used correctly.

## Done this session

- **Upgrade + cleanup.** `meta_fields` instead of the `sam_meta` before-validator;
  `batch(on_timeout="sync")` + `[llm] max_concurrency` instead of the sequential
  fallback; one attempt per budgeted SAM request (explicit `max_attempts=1`);
  `ttl_seconds=0` dropped from the fixture recorder; `public-data` dropped from
  the workflow cache (run-agent.yml restores the data branch); per-tier
  temperature (`config/models.toml`: fast = 0). The committed SAM ledger stays: it
  is what binds on a fresh CI checkout.
- **Workflow.** `agent-grants.yml` calls `run-agent.yml@v0.3.0`; top-level
  `permissions: {}`, the job grants `contents: write` + `issues: write` (ops
  alerts). New `evals.yml` calls `run-evals.yml@v0.3.0` on PRs touching
  `agents/`, `evals/`, `config/`, `pyproject.toml` or `uv.lock`
  (`contents: read`, `max_usd: "1.00"`, threshold 0.20).
- **agents-hub fixes.** No Anthropic key → status `ok`, cached scores/research,
  template summaries, a warning (was a crash). `delta_format`: this agent
  publishes `null` everywhere (the report was about real-estate-agent). A test now
  pins every `format`/`delta_format` to agents-core's `StatFormat`.
- **Warnings and alerts.** `meta.warnings` for a missing/rejected/expired SAM key,
  a SAM window cut short, a Grants.gov failure, MAX_RUN_USD deferrals, stopped
  research loops. `ctx.alert` (ops-alert issue) for a rejected/expired/expiring
  SAM key (`[sam_key] expires_on`), a SAM budget/429 cut-off, MAX_RUN_USD.
- **(A) Tracing.** agents-core traces every run. The agent adds `custom` spans for
  score candidates, description fetches, summaries, bid research and each research
  loop. The live run published `trace.json` (22 spans) and `trace_summary`.
- **(B) Evals.** `evals/grants/suites.py` (`grants-scoring`, `grants-summaries`,
  `grants-research`) on `agents_core.evals`, appending to `evals/history.jsonl`.
  `run_evals.py` removed; old dated results kept.
- **(C) Bid research.** `agents/grants/research.py` (+ `attachments.py`,
  `usaspending.py`): `AgentLoop` with `get_opportunity`, `list_attachments`,
  `read_attachment` (PDF via pypdf, DOCX via the zip XML, at most 3 per opportunity,
  budgeted conditional downloads), `usaspending_prior_awards`,
  `grants_gov_detail`, `finish`. Limits: 10 steps, $0.12, 240 s per opportunity,
  at most 4 SAM requests per run, at most 3 opportunities per run. The guard
  covers numbers, dates and award ids, with a template fallback. Cached per
  content hash. Published as `top_matches[i].research` / `all.json`
  `has_research`, documented in SPEC_GRANTS.md §6.3. Agent `schema_version` is
  now 1.1.0; the schema snapshot diff is additive only. Replay tests use
  trajectories recorded live (`tests/grants/test_research_replay.py`), and
  `python -m evals.grants.demo` prints a replayed brief.
- **(D)** `docs/case-studies.md`: five incidents with commit links.
- **(E)** README: Highlights, Demo and Evals sections.

## Not reachable from this environment

**api.usaspending.gov** and **sam.gov** (attachment downloads) are denied by
this cloud environment's network policy (proxy 403 on CONNECT). The
`usaspending_prior_awards` and `read_attachment` tools are built and evaluated
against hand-built fixtures in the documented response shapes
(`evals/grants/research/`, made by `tools/make_research_fixtures.py`). To
exercise them live here, add both hosts to the environment's allowed domains
(environment settings → Network access). GitHub Actions runners can reach both,
so production is unaffected. api.sam.gov, api.grants.gov and api.anthropic.com
are reachable. The first live research runs will be the first real test of
the USAspending query shape and agency-name mapping (`usaspending.agency_name`).

## Needed from agents-core (not modified here; local workarounds noted)

1. Minor: `run_suite` records `git rev-parse HEAD`, so evals run on an
   uncommitted tree are attributed to the previous commit.

(Resolved by v0.2.0/v0.3.0/v0.3.1 and removed here: sync-call `temperature`
(`llm_compat.py`), an `LLMJudge` temperature hook, a total eval cap, agent meta fields, data-branch
restore, per-host retry control, a warnings channel + issue helper, UTC budget
day.)

## What you need to do by hand

1. **Secrets** in `Kghaffari26/sam-agent` → Settings → Secrets → Actions:
   `SAM_API_KEY`, `ANTHROPIC_API_KEY`, `SITE_DISPATCH_TOKEN` (optional).
   `GITHUB_TOKEN` for ops alerts is automatic (the job grants `issues: write`).
2. **Set `[sam_key] expires_on`** in `config/grants.toml` to your key's expiry
   date (SAM.gov → Account Details) to get an alert 14 days ahead.
3. **Review the eval labels**: `evals/grants/labels_proposed.json`, and the
   research labels in `evals/grants/research/cases.json` (`r3-nsf-grant`
   especially).
4. Optionally allow `api.usaspending.gov` and `sam.gov` in this cloud
   environment's network settings for live research tests here.
5. Point agents-hub at the new additive fields (`research`, `has_research`,
   `meta.warnings`) when it wants to show them; nothing breaks if it doesn't.

## Notes / next steps

- No live opportunity has reached Pursue yet (best fit 73), so research hasn't
  run on production data. The first Pursue match will use up to 4 SAM requests
  for attachments. With the basic key's ~10/day that competes with description
  fetches, and the ledger enforces both.
- Calibrate the judges against a few human-scored outputs, then pass
  `adjust=report.offset_adjust()`.
- `largest_value` is still null (SAM search results carry no values).
