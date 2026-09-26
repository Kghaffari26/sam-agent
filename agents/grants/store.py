"""Dedupe and the rolling active-opportunity store (SPEC_GRANTS.md §5.1, §5.6).

Pipeline: normalize -> dedupe -> merge into store -> prune expired.
"""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel
from rapidfuzz import fuzz

from agents.grants.models import Opportunity, Score, Summary

TITLE_SIMILARITY_THRESHOLD = 90.0  # rapidfuzz token_set_ratio is 0-100, spec asks for >= 0.9
_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    lowered = title.lower()
    no_punct = _PUNCT_RE.sub(" ", lowered)
    return _WS_RE.sub(" ", no_punct).strip()


def dedupe_within_source(opportunities: list[Opportunity]) -> list[Opportunity]:
    """Keep the latest `posted_date` per `source_id`, then collapse SAM amendments
    that share a `solicitation_number` down to the newest notice."""
    latest_by_source_id: dict[tuple[str, str], Opportunity] = {}
    for opp in opportunities:
        key = (opp.source, opp.source_id)
        existing = latest_by_source_id.get(key)
        if existing is None or opp.posted_date > existing.posted_date:
            latest_by_source_id[key] = opp

    by_solicitation: dict[str, Opportunity] = {}
    result: list[Opportunity] = []
    for opp in sorted(latest_by_source_id.values(), key=lambda o: o.posted_date):
        if opp.source == "sam" and opp.solicitation_number:
            prior = by_solicitation.get(opp.solicitation_number)
            if prior is not None:
                result.remove(prior)
            by_solicitation[opp.solicitation_number] = opp
        result.append(opp)
    return result


def _find_cross_source_duplicate(
    candidate: Opportunity, existing: Iterable[Opportunity]
) -> Opportunity | None:
    if not candidate.agency:
        return None
    candidate_title = normalize_title(candidate.title)
    candidate_agency = candidate.agency.strip().lower()
    for other in existing:
        if other.source == candidate.source or not other.agency:
            continue
        if other.agency.strip().lower() != candidate_agency:
            continue
        score = fuzz.token_set_ratio(candidate_title, normalize_title(other.title))
        if score >= TITLE_SIMILARITY_THRESHOLD:
            return other
    return None


def dedupe_across_sources(opportunities: list[Opportunity]) -> list[Opportunity]:
    """Merge same-posting duplicates across SAM/Grants.gov (rare): same normalized
    title (token-set similarity >= 0.9) and matching agency. Keeps the newer one."""
    kept: list[Opportunity] = []
    for opp in opportunities:
        duplicate = _find_cross_source_duplicate(opp, kept)
        if duplicate is None:
            kept.append(opp)
        elif opp.posted_date > duplicate.posted_date:
            kept.remove(duplicate)
            kept.append(opp)
    return kept


def dedupe(opportunities: list[Opportunity]) -> list[Opportunity]:
    return dedupe_across_sources(dedupe_within_source(opportunities))


def is_expired(opp: Opportunity, *, today: date, forecast_max_age_days: int) -> bool:
    if opp.deadline is not None:
        return opp.deadline.date() < today
    return (today - opp.posted_date).days > forecast_max_age_days


def merge_into_store(
    store_items: dict[str, Opportunity],
    fetched: list[Opportunity],
    *,
    now: datetime,
) -> tuple[dict[str, Opportunity], set[str], set[str]]:
    """Merge freshly fetched, already-deduped opportunities into the store.

    Returns `(updated_store, new_ids, changed_ids)`. `new_ids` are opportunities
    not previously in the store; `changed_ids` are ones whose `content_hash`
    differs from what was stored (amendment, deadline extension, etc.) --
    SPEC_GRANTS.md §5.6's `is_new` / `changed`.
    """
    updated = dict(store_items)
    new_ids: set[str] = set()
    changed_ids: set[str] = set()

    for opp in fetched:
        prior = updated.get(opp.id)
        merged = opp.model_copy()
        if prior is None:
            merged.first_seen_at = now
            new_ids.add(opp.id)
        else:
            merged.first_seen_at = prior.first_seen_at
            if merged.content_hash != prior.content_hash:
                changed_ids.add(opp.id)
        merged.last_seen_at = now
        updated[opp.id] = merged

    return updated, new_ids, changed_ids


def prune_store(
    store_items: dict[str, Opportunity],
    *,
    today: date,
    forecast_max_age_days: int,
    store_max_items: int,
    relevance_by_id: dict[str, int] | None = None,
) -> dict[str, Opportunity]:
    """Drop expired items first, then the lowest-relevance items beyond `store_max_items`."""
    active = {
        id_: opp
        for id_, opp in store_items.items()
        if not is_expired(opp, today=today, forecast_max_age_days=forecast_max_age_days)
    }
    if len(active) <= store_max_items:
        return active

    relevance_by_id = relevance_by_id or {}
    ordered = sorted(active.items(), key=lambda kv: relevance_by_id.get(kv[0], 0), reverse=True)
    return dict(ordered[:store_max_items])


# ---- persistence: data/grants/store.json.gz (SPEC_GRANTS.md §4) -------------------


class StoreEntry(BaseModel):
    """One active opportunity that passed hard filters, with its latest score
    and summary (each carrying its own cache key fields)."""

    opportunity: Opportunity
    relevance: int = 0
    score: Score | None = None
    summary: Summary | None = None


def load_store(path: Path) -> dict[str, StoreEntry]:
    if not path.is_file():
        return {}
    data = json.loads(gzip.decompress(path.read_bytes()))
    return {id_: StoreEntry.model_validate(e) for id_, e in data.get("items", {}).items()}


def save_store(entries: dict[str, StoreEntry], path: Path) -> int:
    """Write the store atomically (gzip, mtime=0 so unchanged data is byte-identical).
    Returns the compressed size in bytes."""
    items = {id_: entries[id_].model_dump(mode="json") for id_ in sorted(entries)}
    payload = json.dumps({"items": items}, sort_keys=True, separators=(",", ":")).encode()
    blob = gzip.compress(payload, mtime=0)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(blob)
    tmp.replace(path)
    return len(blob)
