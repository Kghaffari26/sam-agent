# sam-agent

Daily agents that screen public opportunity feeds (starting with SAM.gov
contracts and Grants.gov grants), score them against a configured business
profile, and publish a ranked, explainable shortlist.

## Status

Early scaffolding. The first agent being built is the **Grants & Contracts
Finder** (`agents/grants/`), specified in
[`docs/specs/SPEC_GRANTS.md`](docs/specs/SPEC_GRANTS.md).

Implemented so far (spec §14, build order step 1):

- `agents/grants/models.py` — normalized `Opportunity`, `Score`, `Summary` models.
- `agents/grants/config.py` — `BusinessProfile` / `GrantsConfig` pydantic models,
  TOML loaders, and profile hashing (for cache invalidation).
- `agents/grants/setasides.py` — SAM.gov set-aside code → label + eligibility rule.
- `agents/grants/eligibility.py` — Grants.gov applicant-type code → entity-type rule.
- `config/business_profile.toml`, `config/grants.toml` — example configuration.
- `tests/grants/` — unit tests for the above.

Not yet built (see `docs/specs/SPEC_GRANTS.md` §14 for the remaining steps):
fetchers, the rolling opportunity store, hard filters, relevance pre-scoring,
LLM rubric scoring, summarization, the published JSON schema, evals, and the
scheduled workflow. A shared `core/` package (LLM batch client, HTTP helpers,
cost tracking, publish helpers, prompt guards) is a dependency of later steps
and does not exist yet either.

## Development

This project uses [`uv`](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync
uv run pytest
```
