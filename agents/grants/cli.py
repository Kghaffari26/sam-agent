"""TEMPORARY standalone CLI, pending `agents_core`'s `Agent`/runner + real
fetchers (task 3). Once those exist, this repo runs as `uv run agents-run
grants --dry-run` through `agents_core`'s registry instead of this module,
and `--dry-run` calls `run_pipeline` against real SAM/Grants.gov fetch
output rather than the demo dataset below -- see CLAUDE.md / STATUS.md.

For now this proves out `run_pipeline` end to end against the eval dataset
(already-normalized `Opportunity` records) so `--dry-run` has *something*
real to fetch-filter-and-prescore, with zero LLM calls, matching the
acceptance criterion in SPEC_GRANTS.md §13.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from agents.grants.config import load_business_profile, load_grants_config
from agents.grants.models import Opportunity
from agents.grants.pipeline import format_report, run_pipeline

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE_PATH = REPO_ROOT / "config" / "business_profile.toml"
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "grants.toml"
DEMO_DATASET_PATH = REPO_ROOT / "evals" / "grants" / "dataset.json"


def load_demo_opportunities(path: Path = DEMO_DATASET_PATH) -> list[Opportunity]:
    records = json.loads(path.read_text())
    return [Opportunity.model_validate(r) for r in records]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch (demo data for now), normalize, filter and pre-score. No LLM calls.",
    )
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE_PATH))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--dataset", default=str(DEMO_DATASET_PATH))
    args = parser.parse_args(argv)

    if not args.dry_run:
        parser.error(
            "only --dry-run is supported until this repo is wired into agents_core's runner"
        )

    profile = load_business_profile(args.profile)
    config = load_grants_config(args.config)
    opportunities = load_demo_opportunities(Path(args.dataset))
    today = datetime.now(UTC).date()

    report = run_pipeline(opportunities, profile, config, today=today)
    print(format_report(report, top_n=config.settings.top_n_summaries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
