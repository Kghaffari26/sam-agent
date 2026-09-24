"""The fetch-free part of the pipeline: dedupe -> hard filters -> relevance,
with no LLM calls (SPEC_GRANTS.md §4, and the --dry-run acceptance criterion).

`run_pipeline` takes an already-normalized list of `Opportunity` so it's
agnostic to where they came from. Once `fetch_sam.py` / `fetch_grants_gov.py`
exist (task 3, blocked on `agents_core.http`), and once this repo is wired
into `agents_core`'s `Agent`/runner, this becomes the `transform` step; for
now, `cli.py` calls it directly against demo data for a working `--dry-run`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from agents.grants.config import BusinessProfile, GrantsConfig
from agents.grants.filters import RejectReason, partition
from agents.grants.models import Opportunity
from agents.grants.relevance import select_candidates
from agents.grants.store import dedupe


@dataclass
class DryRunReport:
    fetched_count: int
    deduped_count: int
    survivor_count: int
    rejected: dict[RejectReason, int]
    candidates: list[tuple[Opportunity, int]]
    below_threshold_count: int


def run_pipeline(
    opportunities: list[Opportunity],
    profile: BusinessProfile,
    config: GrantsConfig,
    *,
    today: date,
) -> DryRunReport:
    deduped = dedupe(opportunities)
    survivors, rejected = partition(deduped, profile, today=today)
    candidates, below_threshold = select_candidates(
        survivors,
        profile,
        relevance_threshold=config.settings.relevance_threshold,
        max_candidates=config.settings.max_llm_scoring_per_run,
    )
    return DryRunReport(
        fetched_count=len(opportunities),
        deduped_count=len(deduped),
        survivor_count=len(survivors),
        rejected=rejected,
        candidates=candidates,
        below_threshold_count=len(below_threshold),
    )


def format_report(report: DryRunReport, *, top_n: int = 20) -> str:
    lines = [
        f"Fetched: {report.fetched_count}  Deduped: {report.deduped_count}",
        f"Survived hard filters: {report.survivor_count}",
        "",
        "Reject reasons:",
    ]
    for reason, count in report.rejected.items():
        lines.append(f"  {reason:<18} {count}")
    lines += [
        "",
        f"Candidates (>= relevance threshold): {len(report.candidates)}",
        f"Below relevance threshold: {report.below_threshold_count}",
        "",
        f"Top {top_n} by relevance (no LLM calls):",
    ]
    for opp, relevance in report.candidates[:top_n]:
        lines.append(f"  [{relevance:3d}] {opp.id:32s} {opp.title}")
    return "\n".join(lines)
