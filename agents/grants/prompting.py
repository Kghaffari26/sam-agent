"""Prompt inputs shared by scoring (§7.2) and summaries (§7.3): the
opportunity payload, code-computed eligibility facts, and the facts the
number guard checks LLM text against.

Everything here is deterministic Python. The facts passed to
`agents_core.guards` are exactly the numbers present in the prompt input
(numeric fields, plus every number written inside the input's text), so the
guard allows a number in the output only if the input contained it.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from agents_core.guards import extract_numbers

from agents.grants.config import CLEARANCE_RANK, BusinessProfile
from agents.grants.eligibility import is_eligible_for_grant
from agents.grants.models import Opportunity
from agents.grants.normalize import EASTERN
from agents.grants.setasides import is_eligible_for_set_aside

DESCRIPTION_PROMPT_CHARS = 4000  # of the stored 8,000, to keep the fast tier cheap

CLEARANCE_RE = re.compile(
    r"\b(?:top[\s-]?secret|TS/SCI|secret|security clearance|clearance)\b", re.I
)

NOTICE_TYPE_LABELS = {
    "solicitation": "Solicitation",
    "combined_synopsis_solicitation": "Combined Synopsis/Solicitation",
    "presolicitation": "Presolicitation",
    "sources_sought": "Sources Sought",
    "special_notice": "Special Notice",
    "grant_posted": "Grant (posted)",
    "grant_forecasted": "Grant (forecasted)",
}


def days_left(opp: Opportunity, today: date) -> int | None:
    if opp.deadline is None:
        return None
    return (opp.deadline.astimezone(EASTERN).date() - today).days


def human_date(dt: datetime) -> str:
    """e.g. `October 15, 2026 4:00 PM EDT` -- the form a summary may quote."""
    local = dt.astimezone(EASTERN)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%B')} {local.day}, {local.year} {hour}:{local:%M %p %Z}"


def place_text(opp: Opportunity) -> str | None:
    parts = [p for p in (opp.place_city, opp.place_state) if p]
    return ", ".join(parts) if parts else None


def value_in_range(opp: Opportunity, profile: BusinessProfile) -> str:
    if opp.value_amount is None:
        return "unknown"
    low = profile.min_value if profile.min_value is not None else float("-inf")
    high = profile.max_value if profile.max_value is not None else float("inf")
    return "yes" if low <= opp.value_amount <= high else "no"


def eligibility_facts(opp: Opportunity, profile: BusinessProfile) -> dict[str, Any]:
    """§7.2: computed by code and given to the model as ground truth."""
    return {
        "set_aside_ok": is_eligible_for_set_aside(opp.set_aside_code, profile),
        "entity_type_ok": is_eligible_for_grant(opp.eligibility_codes, profile.entity_type),
        "clearance_required_detected": bool(
            opp.description_text and CLEARANCE_RE.search(opp.description_text)
        ),
        "profile_clearance": profile.clearance,
        "sam_registered": profile.sam_registered,
        "value_in_range": value_in_range(opp, profile),
    }


def opportunity_payload(opp: Opportunity, profile: BusinessProfile, today: date) -> dict[str, Any]:
    """The per-item user-message JSON for scoring and summaries."""
    description = opp.description_text
    if description:
        description = f"<<<UNTRUSTED>>> {description[:DESCRIPTION_PROMPT_CHARS]} <<<END>>>"
    value = None
    if opp.value_amount is not None:
        value = {"kind": opp.value_kind, "amount": opp.value_amount, "floor": opp.value_floor}
    return {
        "opportunity": {
            "title": opp.title,
            "kind": opp.kind,
            "agency": opp.agency,
            "sub_agency": opp.sub_agency,
            "notice_type": NOTICE_TYPE_LABELS.get(opp.notice_type, opp.notice_type),
            "solicitation_number": opp.solicitation_number,
            "naics": opp.naics,
            "psc": opp.psc,
            "aln": opp.aln,
            "set_aside": opp.set_aside_label,
            "posted_date": opp.posted_date.isoformat(),
            "deadline": human_date(opp.deadline) if opp.deadline else None,
            "days_left": days_left(opp, today),
            "value": value,
            "place": place_text(opp),
            "description_fetched": opp.description_fetched,
            "description": description,
        },
        "eligibility_facts": eligibility_facts(opp, profile),
    }


def _strings(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, list | tuple):
        return [s for v in obj for s in _strings(v)]
    return []


def _numbers(obj: Any) -> list[float]:
    if isinstance(obj, bool) or obj is None:
        return []
    if isinstance(obj, int | float):
        return [float(obj)]
    if isinstance(obj, dict):
        return [n for v in obj.values() for n in _numbers(v)]
    if isinstance(obj, list | tuple):
        return [n for v in obj for n in _numbers(v)]
    return []


def input_facts(*inputs: Any) -> list[float]:
    """Every number in the prompt input: numeric fields, plus every number
    written inside its strings (a title's "Phase 2", a description's "$250K",
    NAICS/PSC codes as digits) at both its written and scaled magnitude."""
    facts = [n for obj in inputs for n in _numbers(obj)]
    for text in (s for obj in inputs for s in _strings(obj)):
        for token in extract_numbers(text):
            facts.append(token.value)
            if token.scale != 1.0:
                facts.append(token.value * token.scale)
        facts.extend(float(d) for d in re.findall(r"\d+", text))
    return sorted(set(facts))


# ---- dates (§7.3: code checks that any YYYY / Month D date appears in the input) ----

_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}
_MONTH_NAMES = "|".join(sorted(_MONTHS, key=len, reverse=True))
_MONTH_DAY_RE = re.compile(r"\b(" + _MONTH_NAMES + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_US_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2}|2100)\b")


def dates_in(text: str) -> tuple[set[tuple[int, int]], set[int]]:
    """(month, day) pairs and years mentioned in `text`, in any common form."""
    month_days: set[tuple[int, int]] = set()
    for m in _MONTH_DAY_RE.finditer(text):
        month_days.add((_MONTHS[m.group(1).lower()], int(m.group(2))))
    for _y, mo, d in _ISO_DATE_RE.findall(text):
        month_days.add((int(mo), int(d)))
    for mo, d, _y in _US_DATE_RE.findall(text):
        month_days.add((int(mo), int(d)))
    years = {int(y) for y in _YEAR_RE.findall(text)}
    return month_days, years


def unsupported_dates(output_text: str, input_text: str) -> list[str]:
    """Dates in `output_text` whose month/day or year never appears in `input_text`."""
    allowed_md, allowed_years = dates_in(input_text)
    bad: list[str] = []
    for m in _MONTH_DAY_RE.finditer(output_text):
        if (_MONTHS[m.group(1).lower()], int(m.group(2))) not in allowed_md:
            bad.append(m.group(0))
    for y in _YEAR_RE.findall(output_text):
        if int(y) not in allowed_years:
            bad.append(y)
    return bad


def clearance_rank_required(text: str) -> int | None:
    """The clearance level a red flag/description says is required, if any."""
    lowered = text.lower()
    if "top secret" in lowered or "top-secret" in lowered or "ts/sci" in lowered:
        return CLEARANCE_RANK["top_secret"]
    if re.search(r"\bsecret\b", lowered) or re.search(r"\bclearance\b", lowered):
        if "public trust" in lowered:
            return CLEARANCE_RANK["public_trust"]
        return CLEARANCE_RANK["secret"]
    if "public trust" in lowered:
        return CLEARANCE_RANK["public_trust"]
    return None
