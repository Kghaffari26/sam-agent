# Case studies

Five real incidents from building this agent, each as problem → how it was
caught → fix → result. Sources: [`DECISIONS.md`](../DECISIONS.md),
[`STATUS.md`](../STATUS.md) and the git history. Numbers are from the logged
runs (`data/costs.jsonl`, `data/eval_costs.jsonl`, `evals/`).

## 1. Speculative red flags capped good matches at fit 20

**Problem.** The rubric's code-side math caps an opportunity at fit 20 when a
red flag is a hard blocker, such as a clearance above the profile's. On the
first live run (2026-09-26), the model added red flags like *"DoD contract; no
clearance held"* and *"requires security clearance likely"* to notices that
never mentioned a clearance, and the cap turned plausible matches into Pass.

**How it was caught.** A live run: reading the published `red_flags` next to
the notices they came from.

**Fix.** The hard-blocker check now needs a stated requirement (*requires,
must, needed, only*) with no hedge (*likely, may, unclear, verify, ...*); the
score prompt (bumped to `v2`) says to name a clearance only when the input
states one; and cached scores are re-finalized in code every run
(`scoring.refinalize`), so cap changes apply without LLM calls.
`test_speculative_clearance_flags_do_not_cap` pins the four real phrasings.

**Result.** The second live run rescored the 9 candidates in one batch for
$0.0094 with no false caps. Commit
[`6ecbd71`](https://github.com/Kghaffari26/sam-agent/commit/6ecbd71463587dc020ffe553fb8740d51b10b060).

## 2. httpx silently dropped the SAM description URL's query string

**Problem.** SAM returns each notice's description as a URL with its own query
(`.../noticedesc?noticeid=...`). httpx **replaces** a URL's query when
`params=` is passed, so adding `api_key` dropped `noticeid`, and every
description fetch would have hit the endpoint with no notice id. On a basic
key (about 10 requests a day), each wasted call is a tenth of the day.

**How it was caught.** A unit test against the fixture server, before any live
SAM call: `test_description_fetch_costs_one_request_and_404_is_empty` asserts
the request carries both `noticeid` and `api_key`.

**Fix.** `fetch_description` splits the URL's own query into `params` and
sends the base URL.

**Result.** Both description fetches on the first live run succeeded (2 of 2
requests), and the 2026-09-27 run fetched two more with no failures. Commit
[`6ecbd71`](https://github.com/Kghaffari26/sam-agent/commit/6ecbd71463587dc020ffe553fb8740d51b10b060).

## 3. A missing Anthropic key took the whole section down

**Problem.** Without `ANTHROPIC_API_KEY`, `agents-run grants` crashed: scoring
caught `LLMError`, but the SDK client's missing-key `RuntimeError` escaped, and
the runner published a `failed` manifest entry. A missing secret removed the
grants section from the site instead of degrading it.

**How it was caught.** Downstream: agents-hub's integration against real
agent output, listed under "Agent-side fixes needed" in its STATUS.md.

**Fix.** `analyze` checks for a key up front (`llm_available`: agents-core's
`llm.client` raises `RuntimeError` without one). With no key it skips scoring,
description fetches and research, publishes cached scores, summaries and
research plus template summaries for the rest, and adds a warning to
`meta.warnings` (new in agents-core v0.2.0). Status stays `ok`.

**Result.** `test_no_anthropic_key_publishes_ok_with_a_warning` and
`test_no_anthropic_key_after_a_keyed_run_keeps_cached_work` run the real
agents-core runner with no key: exit 0, `status: "ok"`, a warning, and the
previous run's top matches still published. Commit
[`4b8c0cc`](https://github.com/Kghaffari26/sam-agent/commit/4b8c0cc44693b9e0e3e93690ce9edb6e38f5681e).

## 4. Borderline stability, then a temperature crash the eval caught

**Problem.** §11 wants at least 90% of items to keep the same recommendation
across two scorings. On 2026-09-26 one sample scored 86.7% (fail) and the
next 96.7%: flips sat on the 55/75 band edges (`sam:m010`: Consider 55 → Pass
50). agents-core v0.1.0 had no sampling control. v0.2.0 added a per-tier
`temperature`, so the fast (scoring) tier now runs at 0. But the first live
eval after the upgrade failed: `Messages.parse() got an unexpected keyword
argument 'temperature'`. agents-core v0.3.0 passes a tier's temperature as a
keyword argument, and the Anthropic SDK it pins (1.8) no longer has one, so
every synchronous call on the fast tier (sync rescoring, guard retries, the eval
judge) would have raised. Batches were unaffected.

**How it was caught.** The eval harness. The first live `grants-research` case
passed all of its trajectory checks, but its LLM-judge scorer errored. A
direct probe for under a cent showed the API accepts `temperature` in the request
body for the fast model (Sonnet 5 rejects it: "deprecated for this model").

**Fix.** The fix stays in this repo, because shared code isn't patched here: `agents/grants/llm_compat.py`
wraps agents-core's own client, moves `temperature` into `extra_body`, and
keeps the same CostTracker. The gap is listed for agents-core in STATUS.md.
`test_llm_compat.py` uses an SDK stand-in that rejects the keyword, as SDK 1.8 does.

**Result.** The 2026-09-27 live run rescored two items synchronously through the
shim at temperature 0, and the `grants-scoring` eval (two Batch API samples of the
40-item set) kept the same recommendation for **96.7%** of the 30 scored items,
all within ±5 points ($0.055). Scored the same way before the change, one sample was
86.7%. agents-core v0.3.1 then fixed the keyword upstream (it sends `temperature`
in `extra_body`), so `llm_compat.py` and the judge subclass were deleted. Commit
[`4b8c0cc`](https://github.com/Kghaffari26/sam-agent/commit/4b8c0cc44693b9e0e3e93690ce9edb6e38f5681e).

## 5. A rescored notice kept its old summary

**Problem.** Cached summaries are validated against each item's content hash
at the start of `analyze`. Fetching a SAM description later in the same run
changes the hash, so the item is rescored, but the summary validated earlier
was still published and stored. The published summary described the notice
without its description text for a run, next to a score that included it.

**How it was caught.** The 2026-09-27 live run: two items were rescored after
their descriptions arrived, and the run's `model_usage` showed zero smart-tier
(summary) tokens. Rescored items should have produced new summaries.

**Fix.** After the description step, `analyze` re-validates the summary and
research caches against the new hashes, so a rescored item gets a fresh
summary (and research, if it's a Pursue match) in the same run.

**Result.** `test_description_fetch_invalidates_cached_summary` fails on the
old code and passes on the fix. The stale summaries in that run's store are
regenerated on the next run (their cache keys no longer match). Commit
[`cec2caf`](https://github.com/Kghaffari26/sam-agent/commit/cec2caff5d6075abdb174bafb003ac65cf705ff9).
