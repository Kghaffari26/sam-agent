"""USAspending.gov prior-award lookups for bid research (public API, no key).

One `POST /api/v2/search/spending_by_award/` per lookup, through agents-core's
Http (cached, rate-limited). Every value a research brief publishes about a prior
award (recipient, amount, dates) is copied from this response in code; the model
only picks which award ids are relevant and writes narrative about them.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from agents_core.http import Http

USA_HOST = "api.usaspending.gov"
AWARD_SEARCH_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
AWARD_PAGE = "https://www.usaspending.gov/award/{}"
MIN_INTERVAL_SECONDS = 0.5
LOOKBACK_YEARS = 5
CONTRACT_TYPES = ["A", "B", "C", "D"]  # definitive contracts, purchase/delivery orders, BPA calls
GRANT_TYPES = ["02", "03", "04", "05"]  # block, formula, project grants, cooperative agreements
FIELDS = [
    "Award ID",
    "Recipient Name",
    "Award Amount",
    "Start Date",
    "End Date",
    "Awarding Agency",
    "Awarding Sub Agency",
    "Description",
    "generated_internal_id",
]
DESCRIPTION_CHARS = 200
_SMALL_WORDS = {"of", "the", "and", "for", "on", "in"}


def agency_name(agency: str) -> str:
    """SAM's upper-case agency names -> USAspending's agency names, e.g.
    "VETERANS AFFAIRS, DEPARTMENT OF" -> "Department of Veterans Affairs",
    "DEPT OF DEFENSE" -> "Department of Defense", "DEFENSE HEALTH AGENCY (DHA)" ->
    "Defense Health Agency" (a trailing acronym in parentheses is dropped)."""
    name = " ".join(re.sub(r"\s*\([^)]*\)\s*$", "", agency).split())
    if name.isupper():  # SAM style; USAspending uses title case
        name = " ".join(
            w if i and w in _SMALL_WORDS else w.capitalize()
            for i, w in enumerate(name.lower().split())
        )
    m = re.fullmatch(r"(.+),\s*(Department|Dept\.?) of( the)?", name, re.I)
    if m:
        name = f"Department of {'the ' if m.group(3) else ''}{m.group(1)}"
    return re.sub(r"^Dept\.? of\b", "Department of", name, flags=re.I)


def awarding_agency_filter(
    requested: str | None, *, agency: str | None, sub_agency: str | None
) -> dict[str, str] | None:
    """The USAspending `agencies` filter entry for an agency name the model asked for.

    USAspending matches `name` exactly within the given tier, so a sub-agency sent as
    a toptier agency (e.g. "Department of the Navy", a DoD subtier) matches nothing.
    A name equal to the notice's sub-agency (when that differs from its agency) is a
    subtier filter under the notice's agency; anything else stays a toptier filter.
    No name: the notice's agency."""
    name = agency_name(requested) if requested else None
    top = agency_name(agency) if agency else None
    sub = agency_name(sub_agency) if sub_agency else None
    if name is None:
        name = top
    if name is None:
        return None
    if sub and top and sub.lower() != top.lower() and name.lower() == sub.lower():
        return {"type": "awarding", "tier": "subtier", "name": sub, "toptier_name": top}
    return {"type": "awarding", "tier": "toptier", "name": name}


def award_query(
    *,
    agency: str | None,
    naics: str | None,
    keywords: list[str],
    kind: str,
    today: date,
    limit: int = 8,
    agency_filter: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The award search body. `agency_filter` (from `awarding_agency_filter`) wins
    over `agency`, which is treated as a toptier name."""
    filters: dict[str, Any] = {
        "award_type_codes": GRANT_TYPES if kind == "grant" else CONTRACT_TYPES,
        "time_period": [
            {
                "start_date": (today - timedelta(days=365 * LOOKBACK_YEARS)).isoformat(),
                "end_date": today.isoformat(),
            }
        ],
    }
    if agency_filter is None and agency:
        agency_filter = {"type": "awarding", "tier": "toptier", "name": agency_name(agency)}
    if agency_filter:
        filters["agencies"] = [agency_filter]
    if naics and kind != "grant":
        filters["naics_codes"] = {"require": [naics]}
    words = [k.strip() for k in keywords if len(k.strip()) >= 3][:5]
    if words:
        filters["keywords"] = words
    return {
        "filters": filters,
        "fields": FIELDS,
        "sort": "Award Amount",
        "order": "desc",
        "limit": limit,
        "page": 1,
    }


def _amount(value: Any) -> float | None:
    try:
        return round(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def parse_awards(data: Any) -> list[dict[str, Any]]:
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("USAspending response has no `results` list")
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("Award ID"):
            continue
        internal = r.get("generated_internal_id")
        description = " ".join(str(r.get("Description") or "").split())
        out.append(
            {
                "award_id": str(r["Award ID"]),
                "recipient": r.get("Recipient Name"),
                "amount": _amount(r.get("Award Amount")),
                "start_date": _date(r.get("Start Date")),
                "end_date": _date(r.get("End Date")),
                "awarding_agency": r.get("Awarding Agency"),
                "awarding_sub_agency": r.get("Awarding Sub Agency"),
                "description": description[:DESCRIPTION_CHARS],
                "url": AWARD_PAGE.format(internal) if internal else None,
            }
        )
    return out


def prior_awards(
    http: Http,
    *,
    agency: str | None,
    naics: str | None,
    keywords: list[str],
    kind: str,
    today: date,
    limit: int = 8,
    agency_filter: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(query, awards): the largest matching awards of the last five years."""
    query = award_query(
        agency=agency, naics=naics, keywords=keywords, kind=kind, today=today, limit=limit,
        agency_filter=agency_filter,
    )
    response = http.request("POST", AWARD_SEARCH_URL, json_body=query)
    return query, parse_awards(response.json())
