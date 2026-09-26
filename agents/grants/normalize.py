"""SAM.gov / Grants.gov raw API records -> normalized `Opportunity` (SPEC_GRANTS.md §4).

Deadlines that arrive without a UTC offset are assumed to be U.S. Eastern
(SPEC_GRANTS.md §10) and flagged via `Opportunity.deadline_tz_assumed`.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import date, datetime, time
from typing import Any, Literal
from zoneinfo import ZoneInfo

from agents.grants.models import NoticeType, Opportunity
from agents.grants.setasides import label_for as set_aside_label_for

EASTERN = ZoneInfo("America/New_York")
MAX_DESCRIPTION_CHARS = 8000

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,;:!?])")

SAM_TYPE_TO_NOTICE_TYPE: dict[str, NoticeType] = {
    "Solicitation": "solicitation",
    "Combined Synopsis/Solicitation": "combined_synopsis_solicitation",
    "Presolicitation": "presolicitation",
    "Sources Sought": "sources_sought",
    "Special Notice": "special_notice",
}

GRANTS_GOV_STATUS_TO_NOTICE_TYPE: dict[str, NoticeType] = {
    "posted": "grant_posted",
    "forecasted": "grant_forecasted",
}

GRANTS_GOV_DEFAULT_DEADLINE_TIME = time(23, 59)


def strip_html(text: str | None) -> str | None:
    """Unescape entities and drop tags, collapsing whitespace. None stays None."""
    if not text:
        return None
    unescaped = html.unescape(text)
    without_tags = _TAG_RE.sub(" ", unescaped)
    collapsed = _WS_RE.sub(" ", without_tags).strip()
    tidy = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", collapsed)
    return tidy or None


def truncate_description(text: str | None) -> str | None:
    if text is None:
        return None
    return text[:MAX_DESCRIPTION_CHARS]


def parse_deadline(raw: str | None) -> tuple[datetime | None, bool]:
    """Parse an ISO-ish deadline string. Returns (deadline, tz_assumed).

    A value with no UTC offset is assumed U.S. Eastern, per SPEC_GRANTS.md §10.
    """
    if not raw:
        return None, False
    value = raw.strip()
    if not value:
        return None, False
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=EASTERN), True
    return parsed, False


def parse_mdy_date(raw: str | None) -> date | None:
    """Grants.gov dates are `MM/dd/yyyy`."""
    if not raw:
        return None
    return datetime.strptime(raw, "%m/%d/%Y").date()


def parse_money(raw: Any) -> float | None:
    """Grants.gov amounts arrive as numbers or strings ("305000", "$1,000,000",
    "none", ""). Zero and unparseable values mean "unknown"."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw) or None
    cleaned = str(raw).replace("$", "").replace(",", "").strip()
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value or None


def _detail_section(detail: dict[str, Any]) -> dict[str, Any]:
    return detail.get("synopsis") or detail.get("forecast") or {}


def _first_present(section: dict[str, Any], detail: dict[str, Any], key: str) -> Any:
    value = section.get(key)
    return value if value not in (None, "") else detail.get(key)


def compute_content_hash(fields: dict[str, Any]) -> str:
    """sha256 of the fields that matter for scoring (SPEC_GRANTS.md §4).

    Any change here invalidates cached scores/summaries for the opportunity,
    so only fields that should trigger a rescore belong in `fields`.
    """
    canonical = json.dumps(fields, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _split_parent_path(path: str | None) -> list[str]:
    if not path:
        return []
    return [part.strip() for part in path.split(".") if part.strip()]


def _place_state(place: dict | None) -> str | None:
    if not place:
        return None
    state = place.get("state")
    if isinstance(state, dict):
        return state.get("code") or state.get("name")
    return state


def _place_city(place: dict | None) -> str | None:
    if not place:
        return None
    city = place.get("city")
    if isinstance(city, dict):
        return city.get("name") or city.get("code")
    return city


def sam_hash_fields(
    *,
    title: str,
    description: str | None,
    deadline: datetime | None,
    set_aside_code: str | None,
    naics: list[str],
    psc: str | None,
    agency_path: list[str | None],
    notice_type: str,
) -> dict[str, Any]:
    """The SAM fields whose change should trigger a rescore. Built only from
    normalized fields, so `with_sam_description` can recompute it exactly."""
    return {
        "title": title,
        "description": description,
        "deadline": deadline.isoformat() if deadline else None,
        "set_aside_code": set_aside_code,
        "naics": naics,
        "psc": psc,
        "agency": agency_path,
        "notice_type": notice_type,
    }


def with_sam_description(opp: Opportunity, description_text: str) -> Opportunity:
    """A SAM opportunity with its fetched description (§5.5 #2): same record,
    new `description_text`/`description_fetched`, recomputed `content_hash`.
    `description_text` "" means "fetched, SAM has none"."""
    description = truncate_description(strip_html(description_text))
    fields = sam_hash_fields(
        title=opp.title,
        description=description,
        deadline=opp.deadline,
        set_aside_code=opp.set_aside_code,
        naics=opp.naics,
        psc=opp.psc,
        agency_path=[opp.agency, opp.sub_agency, opp.office],
        notice_type=opp.notice_type,
    )
    return opp.model_copy(
        update={
            "description_text": description,
            "description_fetched": True,
            "content_hash": compute_content_hash(fields),
        }
    )


def sam_description_url(notice_id: str) -> str:
    return f"https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid={notice_id}"


def normalize_sam(
    raw: dict[str, Any],
    *,
    now: datetime,
    description_text: str | None = None,
) -> Opportunity:
    """Normalize one entry of `opportunitiesData` from the SAM v2 search response.

    `description_text` is the already-fetched, HTML body of the notice's
    `description` URL (SPEC_GRANTS.md §3.1), or "" when it was fetched and
    came back empty (404), or None when it hasn't been fetched. The raw
    `description` field itself is only a URL and is not used directly.
    """
    notice_id = raw["noticeId"]
    sam_type = raw.get("type") or raw.get("baseType") or ""
    notice_type = SAM_TYPE_TO_NOTICE_TYPE.get(sam_type)
    if notice_type is None:
        raise ValueError(f"Unsupported SAM notice type: {sam_type!r} (notice {notice_id})")

    try:
        deadline, tz_assumed = parse_deadline(raw.get("responseDeadLine"))
    except ValueError:
        deadline, tz_assumed = None, False

    set_aside_code = raw.get("typeOfSetAside") or None
    set_aside_label = raw.get("typeOfSetAsideDescription") or set_aside_label_for(set_aside_code)

    place = raw.get("placeOfPerformance")
    place_state = _place_state(place)
    place_city = _place_city(place)

    naics = [raw["naicsCode"]] if raw.get("naicsCode") else []
    path_parts = _split_parent_path(raw.get("fullParentPathName"))
    agency = path_parts[0] if path_parts else None
    sub_agency = path_parts[1] if len(path_parts) > 1 else None
    office = path_parts[-1] if len(path_parts) > 2 else None

    description = truncate_description(strip_html(description_text))

    fields_for_hash = sam_hash_fields(
        title=raw["title"],
        description=description,
        deadline=deadline,
        set_aside_code=set_aside_code,
        naics=naics,
        psc=raw.get("classificationCode"),
        agency_path=[agency, sub_agency, office],
        notice_type=notice_type,
    )

    return Opportunity(
        id=f"sam:{notice_id}",
        source="sam",
        source_id=notice_id,
        kind="contract",
        notice_type=notice_type,
        title=raw["title"],
        solicitation_number=raw.get("solicitationNumber"),
        agency=agency,
        sub_agency=sub_agency,
        office=office,
        naics=naics,
        psc=raw.get("classificationCode"),
        set_aside_code=set_aside_code,
        set_aside_label=set_aside_label,
        posted_date=date.fromisoformat(str(raw["postedDate"])[:10]),
        deadline=deadline,
        deadline_tz_assumed=tz_assumed,
        place_state=place_state,
        place_city=place_city,
        value_kind="none",
        value_amount=None,
        value_floor=None,
        url=f"https://sam.gov/opp/{notice_id}/view",
        description_text=description,
        description_fetched=description_text is not None,
        content_hash=compute_content_hash(fields_for_hash),
        first_seen_at=now,
        last_seen_at=now,
    )


def normalize_grants_gov(
    hit: dict[str, Any],
    *,
    now: datetime,
    detail: dict[str, Any] | None = None,
) -> Opportunity:
    """Normalize one `oppHits[]` entry from `search2`, optionally enriched with
    the `data` object of a `fetchOpportunity` call (SPEC_GRANTS.md §3.2).
    """
    opp_id = str(hit["id"])
    status = (hit.get("oppStatus") or "posted").lower()
    notice_type = GRANTS_GOV_STATUS_TO_NOTICE_TYPE.get(status, "grant_posted")

    close_date = parse_mdy_date(hit.get("closeDate"))
    deadline: datetime | None = None
    tz_assumed = False
    if close_date is not None:
        # Grants.gov close dates carry no time-of-day or zone; assume end of
        # day, U.S. Eastern (SPEC_GRANTS.md §10).
        deadline = datetime.combine(close_date, GRANTS_GOV_DEFAULT_DEADLINE_TIME, tzinfo=EASTERN)
        tz_assumed = True

    posted_date = parse_mdy_date(hit.get("openDate")) or now.date()

    aln = [str(a) for a in (hit.get("alnist") or hit.get("cfdaList") or [])]
    eligibility_codes: list[str] = []
    description_text: str | None = None
    value_amount: float | None = None
    value_floor: float | None = None
    value_kind: Literal["award_ceiling", "estimated_total", "award_amount", "none"] = "none"
    title = html.unescape(hit.get("title") or "")

    if detail:
        title = html.unescape(detail.get("opportunityTitle") or "") or title
        # The live API nests these under `synopsis` (posted) or `forecast`
        # (forecasted); older/hand-built shapes had them at the top level.
        section = _detail_section(detail)
        applicant_types = section.get("applicantTypes") or detail.get("applicantTypes") or []
        eligibility_codes = [str(a["id"]) for a in applicant_types if a.get("id")]

        raw_description = (
            section.get("synopsisDesc") or section.get("forecastDesc") or detail.get("description")
        )
        description_text = truncate_description(strip_html(raw_description))

        ceiling = parse_money(_first_present(section, detail, "awardCeiling"))
        floor = parse_money(_first_present(section, detail, "awardFloor"))
        estimated = parse_money(_first_present(section, detail, "estimatedFunding"))
        if ceiling:
            value_amount = ceiling
            value_kind = "award_ceiling"
        elif estimated:
            value_amount = estimated
            value_kind = "estimated_total"
        if floor:
            value_floor = floor

        detail_aln = [c["cfdaNumber"] for c in (detail.get("cfdas") or []) if c.get("cfdaNumber")]
        if detail_aln:
            aln = detail_aln

    fields_for_hash = {
        "title": title,
        "description": description_text,
        "deadline": deadline.isoformat() if deadline else None,
        "eligibility_codes": sorted(eligibility_codes),
        "aln": sorted(aln),
        "agency": hit.get("agency") or hit.get("agencyCode"),
        "notice_type": notice_type,
        "value_amount": value_amount,
    }

    return Opportunity(
        id=f"gg:{opp_id}",
        source="grants_gov",
        source_id=opp_id,
        kind="grant",
        notice_type=notice_type,
        title=title,
        solicitation_number=hit.get("number"),
        agency=hit.get("agency") or hit.get("agencyCode"),
        aln=aln,
        eligibility_codes=eligibility_codes,
        posted_date=posted_date,
        deadline=deadline,
        deadline_tz_assumed=tz_assumed,
        value_kind=value_kind,
        value_amount=value_amount,
        value_floor=value_floor,
        url=f"https://www.grants.gov/search-results-detail/{opp_id}",
        description_text=description_text,
        description_fetched=detail is not None,
        content_hash=compute_content_hash(fields_for_hash),
        first_seen_at=now,
        last_seen_at=now,
    )
