# Status

_Updated 2026-09-26 (session wiring in agents-core v0.1.0)._

## Summary

Every task that was blocked on agents-core is built and has run live:
`uv run agents-run grants [--dry-run]` works through agents-core's entry-point
registry. It fetches SAM.gov (budgeted) and Grants.gov, scores with the LLM
rubric via the Batch API, writes guarded top-20 summaries, and publishes the
full data-branch contract to `public-data/`.

- **Tests: 203 passing, `ruff check .` clean** (all HTTP mocked; no network in tests).
- **agents-core:** tag `v0.1.0` → commit `b0a292daa0d669bcecc7f28e2cf9aa50f49843e3` (locked in `uv.lock`).
- **SAM.gov requests used this session: 3 of the 3 allowed** (ledger:
  `data/grants/state.json` → `sam.requests["2026-09-26"] = 3`): 1 window
  search (the dry run; later runs reused it from agents-core's HTTP cache) + 2
  description fetches (first real run).
- **Anthropic spend this session: $0.2321** = agent runs $0.0719
  (`data/costs.jsonl`) + evals $0.1602 (`data/evals_costs.jsonl`, agent
  `grants-evals`, kept out of the published costs summary).

## Live runs (2026-09-26, `--lookback-days=1 --sam-request-budget=3`)

| Run | What happened | LLM calls | Cost (`data/costs.jsonl`) | SAM requests |
|---|---|---|---|---|
| dry run | SAM window 09/25-09/26: 473 notices (1 page); Grants.gov 354 unique hits, 60 details; reject table + top 20 printed | 0 | $0 | 1 (search) |
| real run 1 | 9 candidates scored (1 batch), top 9: 2 SAM descriptions fetched then 2 sync rescores, 9 summaries (all `llm`, 0 guard failures) | 20 | **$0.0625** | 2 (descriptions) |
| real run 2 | after the red-flag cap fix + score prompt v2: 9 rescored in 1 batch; all 9 summaries served from cache | 9 | $0.0094 | 0 |
| rerun 3 (immediate) | **0 LLM calls, 0 description fetches, 0 SAM requests**; exposed a bug: the SAM search wasn't read from the free HTTP cache once the budget was used up (fixed) | 0 | $0 | 0 |
| rerun 4 (immediate, after fix) | SAM search served from cache (473 notices), 9 scores + 9 summaries from cache | **0** | **$0** | **0** |

Output sizes (rerun 4): `latest.json` 23,686 B (limit ~200 KB), `all.json`
230,996 B, 486 rows (limit ~1 MB), `history/2026-09-26.json` 23,686 B,
`manifest-entry.json` 918 B, `costs-summary.json` 114 B, `schema.json`
11,233 B. `data/grants/store.json.gz` 131 KB (target ≤ 2 MB).

Latest headline: "0 new matches today; 3 close within 14 days. Top: PRIMED-AI:
Data-to-Model Academic-Industrial Partnerships (D2M-AIP) for Precisi… (National
Institutes of Health), fit 73." 7 active matches (Pursue/Consider), 486 active
items. `meta.sam_budget_exhausted` is `true` because the session capped SAM at
3 requests.

## Done this session

- **(a) agents-core for everything shared.** HTTP (`ctx.http`: retries,
  2 req/s Grants.gov policy, cache, SAM `HostPolicy(daily_budget)`), LLM
  (`ctx.llm`: batch, structured, sync), costs, guards
  (`fields_guard`/`verify_numbers`/`extract_numbers`), publish, runner. There
  are no local copies, and `anthropic` isn't imported anywhere here. The SAM
  budget is enforced by agents-core's per-host daily budget, tightened to the
  committed ledger by `fetch_sam.SamBudget` so even Http's own retries stop at
  the tighter limit.
- **(b) Registration.** `[project.entry-points."agents_core.agents"] grants =
  "agents.grants.agent:AGENT"`. The temporary `cli.py` is deleted.
- **(c) Fetchers.**
  - `fetch_sam.py`: one window per run from `last_posted_to` minus 1 day of
    overlap, a 1-year max, pagination only past 1,000 and only within budget,
    404 treated as empty without advancing the window, 401/403 and 429
    handled, and each description fetch counted as one request.
    `max_sam_description_fetches` is a per-UTC-day cap.
  - `fetch_grants_gov.py`: `search2` per `grant_keywords` entry, unioned and
    deduped by id; `fetchOpportunity` for prefiltered new/changed ids, up to
    60 per run, cached permanently by `id|closeDate` in
    `data/grants/grants_gov_details.json.gz`.
- **(d) Scoring and summaries.**
  - `scoring.py`: fast tier over the Batch API, 30-minute timeout then sync
    fallback. Cache key is content_hash + profile_hash + prompt version +
    model. Code clamps and sums the sub-scores, applies the caps (hard blocker
    → 20; no description + low confidence → 70), and sets the Pursue ≥75 /
    Consider ≥55 bands. `reasons`/`red_flags` are guarded, with a
    deterministic scrub fallback.
  - `summarize.py`: smart tier for the top 20, same cache-key scheme,
    number guard plus the §7.3 date check plus non-empty next steps, and a
    template fallback (`templates.py`).
  - Top-20 SAM description fetches rescore an item when its content hash
    changes (§5.5).
- **(e) Publishing.** agents-core writes `public-data/latest.json`,
  `all.json`, `history/`, `manifest-entry.json`, `costs-summary.json` and
  `schema.json`. The §6.1/§6.2 shapes are unchanged, and the schema is pinned
  by `tests/fixtures/grants/schema_snapshot.json`.
- **(f) Workflow.** `.github/workflows/agent-grants.yml` calls
  `Kghaffari26/agents-core/.github/workflows/run-agent.yml@v0.1.0` with
  `agent: grants`, `max_run_usd: "0.50"`, `site_repo: Kghaffari26/agents-hub`
  and `secrets: inherit`. It also sets `extra_args` from the dispatch inputs
  (`--rescore-all`, `--lookback-days`) and `cache_path` (see gaps below). It
  passes actionlint 1.7.7.
- **Fixtures recorded live.**
  - `sam_search_live.json`: the real SAM page, trimmed to 50 notices with
    contacts redacted, recorded from the HTTP cache at no extra request.
  - `grants_gov_search2_live.json`, plus
    `grants_gov_fetch_opportunity_live_{posted,forecasted}.json`.
  - The live shapes differed from the old hand-built fixtures (`cfdaList`,
    fields nested under `synopsis`/`forecast`, `"none"` amounts), and
    `normalize.py` now handles both. `tools/record_fixtures.py` re-records them.
- **Evals re-run against the real scorer.** Labels are still **PROVISIONAL**.
  Results: `evals/results/grants-2026-09-26.json` (second sample); the first
  sample's numbers are below.

  | Check | Result |
  |---|---|
  | Ranking | precision@10 = 1.0, no bad_fit in top 5 — **pass** (both samples) |
  | Hard blockers | clearance item capped at 20; 8(a) item rejected by the set_aside filter — **pass** |
  | Injection resistance | Δfit 1/0/1 and 1/0/0 — **pass** |
  | Summary fidelity | 5/5 top summaries `llm`, all pass the guard and date check, next steps non-empty — **pass** |
  | Stability | sample 1: 96.7% within ±5 but only 86.7% same recommendation (**fail**, needs ≥90%); sample 2: 100% / 96.7% (**pass**). Flips sit on the 55/75 band edges (e.g. `sam:m010`: Consider 55 → Pass 50). Borderline; see gaps. |

  The previous proxy result (`grants-2026-09-24.json`) is kept for history.

## Needed from agents-core (not modified here; local workarounds noted)

1. **Agent-specific `meta` fields.** The runner builds `RunMeta` itself with
   no extension hook, but §6.1 needs `meta.sam_budget_exhausted` and
   `meta.sam_requests_used`. Workaround: `GrantsMeta(RunMeta)` plus a
   before-validator on `GrantsLatest` that moves a `sam_meta` body key into
   `meta`. The published shape matches the spec.
2. **`run-agent.yml` never restores the previous `data` branch into
   `public-data/`.** So `history/` would hold only one snapshot, and
   `manifest-entry.json`'s `last_data_change_at` / `ctx.previous_latest()`
   reset every run. agents-core's `.cache/http` budget file is also lost on a
   fresh checkout. Workaround: `cache_path: .cache/http + public-data`
   (actions/cache, best effort: evicted after 7 days unused). The committed
   `state.json` ledger still guarantees the SAM cap.
3. **Per-host retry control in `HostPolicy`.** Http retries 429/5xx up to 4
   times, and every attempt counts against SAM's ~10/day. The ledger sync
   stops retries at the budget, but one transient 5xx can still burn up to 4
   of the day's requests.
4. **Temperature (or another sampling knob) per tier.** `TierConfig` has
   none. Rubric stability is borderline on the recommendation criterion (see
   evals).
5. **A warning channel for "ok with a warning" (§10, SAM key 401/403).**
   `RunMeta` has no warnings field, and there's no helper for opening a
   GitHub issue. The agent logs the error and records
   `state.sam.key_rejected_at`; the issue at most once a week isn't
   implemented.
6. Minor: agents-core's request-budget day uses `date.today()` (local time)
   while the spec says a UTC day. The two are identical in CI and in this
   container (both UTC).

## What you need to do by hand

1. **Secrets in `Kghaffari26/sam-agent`** (Settings → Secrets → Actions):
   `SAM_API_KEY`, `ANTHROPIC_API_KEY` and, for the agents-hub dispatch,
   `SITE_DISPATCH_TOKEN`. `secrets: inherit` passes whichever exist.
2. **Actions workflow permissions: "Read and write".** run-agent.yml commits
   `data/` and force-pushes the `data` branch; the caller also declares
   `permissions: contents: write`.
3. **Point agents-hub at this repo's `data` branch** (`latest.json`,
   `all.json`, `manifest-entry.json`, `costs-summary.json`, `schema.json`).
4. **Review `evals/grants/labels_proposed.json`.** Still Claude's guesses.
   The eval dataset is still hand-built; real notices from
   `data/grants/store.json.gz` would make better eval items.
5. Optional: a SAM key tied to an entity registration (~1,000/day). Then
   raise `max_sam_description_fetches` in `config/grants.toml` (e.g. 50).
   With the basic key, top-20 SAM items mostly lack descriptions, get scored
   `confidence: low` and are capped at 70.
6. If this landed on the session branch instead of `main`, merge it.

## Notes / next steps

- Only 9 of about 500 active items reach relevance 25 (most SAM notices
  aren't IT NAICS; grants have no NAICS). Grants now also count
  `grant_keywords` (see DECISIONS.md). Grants.gov details backfill at 60 per
  run, so a few more grants will qualify over the next runs.
- `largest_value` is null today: no match has a known value (SAM search
  results carry none).
- The first scheduled CI run will resume from this session's committed
  `data/` (store with cached scores and summaries, state with today's
  ledger).
