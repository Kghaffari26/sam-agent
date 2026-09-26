"""Grants.gov fetcher: `search2` per keyword, `fetchOpportunity` with a
permanent detail cache (SPEC_GRANTS.md §3.2, §10).

All HTTP goes through `agents_core.http.Http` (retries, the 2 req/s host
policy `GrantsAgent` registers, and agents-core's short-lived on-disk cache).
On top of that, fetched details are kept in `data/grants/grants_gov_details.json.gz`
keyed by `id|closeDate`, so a detail is fetched once per id and close date,
ever: a changed close date (an extension) refetches it.
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
from typing import Any

from agents_core.http import Http

log = logging.getLogger(__name__)

GG_HOST = "api.grants.gov"
SEARCH_URL = "https://api.grants.gov/v1/api/search2"
DETAIL_URL = "https://api.grants.gov/v1/api/fetchOpportunity"
HEADERS = {"User-Agent": "sam-agent/0.1 (+https://github.com/Kghaffari26/sam-agent)"}
MIN_INTERVAL_SECONDS = 0.5  # at most 2 requests per second (§3.2)
MAX_DETAIL_FAILURES = 5  # stop detail fetches for the run after this many errors

# The parts of a fetchOpportunity `data` object normalize.py reads; everything
# else (attachments, packages, history) is dropped before caching.
_SECTION_KEYS = (
    "synopsisDesc",
    "forecastDesc",
    "awardCeiling",
    "awardFloor",
    "estimatedFunding",
    "applicantTypes",
    "costSharing",
    "responseDate",
    "responseDateDesc",
)


class GrantsGovSchemaError(RuntimeError):
    """The API answered, but not in the shape this fetcher knows (§10)."""


def _check_envelope(data: Any, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise GrantsGovSchemaError(f"{what}: response is not a JSON object")
    if data.get("errorcode") not in (0, None):
        raise GrantsGovSchemaError(f"{what}: errorcode {data.get('errorcode')}: {data.get('msg')}")
    inner = data.get("data")
    if not isinstance(inner, dict):
        raise GrantsGovSchemaError(f"{what}: missing `data` object")
    return inner


def search_keyword(
    http: Http, keyword: str, *, opp_statuses: str, rows: int
) -> tuple[list[dict[str, Any]], int]:
    """One `search2` call. Returns (oppHits, hitCount)."""
    body = {"keyword": keyword, "oppStatuses": opp_statuses, "rows": rows, "startRecord": 0}
    response = http.request("POST", SEARCH_URL, json_body=body, headers=HEADERS)
    inner = _check_envelope(response.json(), f"search2 {keyword!r}")
    hits = inner.get("oppHits")
    if not isinstance(hits, list):
        raise GrantsGovSchemaError(f"search2 {keyword!r}: missing data.oppHits")
    return hits, int(inner.get("hitCount") or 0)


def search_all(
    http: Http, keywords: list[str], *, opp_statuses: str, rows: int
) -> list[dict[str, Any]]:
    """One `search2` per keyword, unioned and deduped by `id` (first hit wins)."""
    by_id: dict[str, dict[str, Any]] = {}
    for keyword in keywords:
        hits, hit_count = search_keyword(http, keyword, opp_statuses=opp_statuses, rows=rows)
        log.info("grants.gov %r: %d hits (of %d)", keyword, len(hits), hit_count)
        for hit in hits:
            by_id.setdefault(str(hit["id"]), hit)
    return list(by_id.values())


def detail_key(hit: dict[str, Any]) -> str:
    return f"{hit['id']}|{hit.get('closeDate') or ''}"


def trim_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Keep only what normalize_grants_gov reads, so the cache stays small."""
    out: dict[str, Any] = {"opportunityTitle": detail.get("opportunityTitle")}
    for name in ("synopsis", "forecast"):
        section = detail.get(name)
        if isinstance(section, dict):
            out[name] = {k: section[k] for k in _SECTION_KEYS if k in section}
    for key in ("applicantTypes", "awardCeiling", "awardFloor", "estimatedFunding", "description"):
        if key in detail:
            out[key] = detail[key]
    out["cfdas"] = [
        {"cfdaNumber": c["cfdaNumber"]}
        for c in (detail.get("cfdas") or [])
        if isinstance(c, dict) and c.get("cfdaNumber")
    ]
    return out


def fetch_detail(http: Http, opportunity_id: str | int) -> dict[str, Any]:
    body = {"opportunityId": int(opportunity_id)}
    response = http.request("POST", DETAIL_URL, json_body=body, headers=HEADERS)
    return trim_detail(_check_envelope(response.json(), f"fetchOpportunity {opportunity_id}"))


class DetailCache:
    """`id|closeDate` -> trimmed fetchOpportunity detail, gzip JSON on disk."""

    def __init__(self, entries: dict[str, dict[str, Any]] | None = None) -> None:
        self.entries = entries or {}

    @classmethod
    def load(cls, path: Path) -> DetailCache:
        if not path.is_file():
            return cls()
        return cls(json.loads(gzip.decompress(path.read_bytes())))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.entries, sort_keys=True, separators=(",", ":")).encode()
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(gzip.compress(payload, mtime=0))
        tmp.replace(path)

    def get(self, hit: dict[str, Any]) -> dict[str, Any] | None:
        return self.entries.get(detail_key(hit))

    def put(self, hit: dict[str, Any], detail: dict[str, Any]) -> None:
        self.entries[detail_key(hit)] = detail

    def prune(self, seen_hits: list[dict[str, Any]]) -> None:
        """Drop entries for ids/close dates no longer returned by any search."""
        keep = {detail_key(h) for h in seen_hits}
        self.entries = {k: v for k, v in self.entries.items() if k in keep}


def fetch_details(
    http: Http, hits: list[dict[str, Any]], cache: DetailCache, *, max_fetches: int
) -> int:
    """Fetch details for `hits` (already prefiltered and ordered by priority)
    that aren't cached, up to `max_fetches`. Returns the number fetched. A
    failure on one id is logged and skipped; it's retried next run."""
    fetched = failures = 0
    for hit in hits:
        if fetched >= max_fetches or failures >= MAX_DETAIL_FAILURES:
            break
        if cache.get(hit) is not None:
            continue
        try:
            detail = fetch_detail(http, hit["id"])
        except Exception as e:  # one bad record shouldn't fail the portion
            log.warning("grants.gov detail %s failed: %s", hit["id"], e)
            failures += 1
            continue
        cache.put(hit, detail)
        fetched += 1
    return fetched
