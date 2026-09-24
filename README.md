# sam-agent

The **Grants & Contracts Finder** agent: finds federal contract opportunities
(SAM.gov) and grant opportunities (Grants.gov) that fit a configured business
profile, screens them with deterministic rules, scores the survivors with an
LLM rubric, and publishes a ranked, explainable shortlist for a website.

Full spec: [`docs/specs/SPEC_GRANTS.md`](docs/specs/SPEC_GRANTS.md). See
[`CLAUDE.md`](CLAUDE.md) for repo conventions and [`STATUS.md`](STATUS.md) /
[`DECISIONS.md`](DECISIONS.md) for a running log of what's built and why.

## Architecture (multi-repo)

This repo holds **only** the grants agent. Shared code (the LLM batch client,
HTTP helpers with request budgets, cost tracking, publish helpers, and the
number guard) lives in a separate [`agents-core`](https://github.com/Kghaffari26/agents-core)
package and is imported as `agents_core`. The agent registers itself through
`agents_core`'s agent registry as `grants` and runs via `uv run agents-run
grants [--dry-run]`. Published output goes to `public-data/`, following
`agents-core`'s data-branch contract, and the site that reads it lives in
`Kghaffari26/agents-hub`.

As of this writing, `agents-core` is not yet installable in that shape (see
STATUS.md's "Needed from agents-core") — this repo currently builds and
tests everything that doesn't require it, and will wire in the dependency
once it lands.

```
sam-agent/
├── config/
│   ├── business_profile.toml   # NAICS, keywords, set-asides, thresholds
│   └── grants.toml             # run settings, SAM/Grants.gov knobs
├── agents/grants/
│   ├── models.py                # Opportunity, Score, Summary
│   ├── config.py                 # profile/config loaders + profile hashing
│   ├── setasides.py               # SAM set-aside code -> label + eligibility
│   ├── eligibility.py             # Grants.gov applicant code -> entity type
│   ├── normalize.py               # SAM/Grants.gov raw JSON -> Opportunity
│   ├── store.py                   # dedupe, merge, prune the rolling store
│   ├── filters.py                 # deterministic hard filters (§5.2)
│   ├── relevance.py               # deterministic 0-100 pre-score (§5.3)
│   ├── fetch_sam.py                (planned, needs agents_core.http)
│   ├── fetch_grants_gov.py         (planned)
│   ├── scoring.py                  (planned, needs agents_core.llm)
│   ├── summarize.py                (planned)
│   └── schema.py                   (planned: latest.json / all.json, §6)
├── tests/grants/                # unit tests, no live network calls
├── tests/fixtures/grants/       # recorded/hand-built API response fixtures
├── evals/grants/                # labeled eval set + eval harness (§11)
└── .github/workflows/agent-grants.yml
```

## Development

Python 3.11+, managed with [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest              # unit tests (all mocked/fixture-based, no network)
ruff check .                # lint
uv run python -m evals.grants.run_evals   # provisional eval run, see evals/grants/
```

No live network calls happen in tests. `tests/fixtures/grants/` is hand-built
to match the documented SAM v2 / Grants.gov `search2`+`fetchOpportunity`
response shapes (this environment's egress policy blocks `api.grants.gov`,
so a live recording pass wasn't possible yet — see DECISIONS.md).
