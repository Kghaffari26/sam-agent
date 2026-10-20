"""Deterministic hard filters, applied before any LLM call (SPEC_GRANTS.md §5.2).

Rules run in order; the first one an opportunity fails is its reject reason.
Reject counts by reason code are published for transparency (§6, `stats.rejected`).
The profile's `[filters]` table (§5.2a: SAM.gov's own search filters plus a few
it lacks) adds the reasons after `negative_keyword`.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from agents.grants.config import BusinessProfile
from agents.grants.eligibility import is_eligible_for_grant
from agents.grants.models import Opportunity
from agents.grants.setasides import SOLE_SOURCE_CODES, is_eligible_for_set_aside

RejectReason = Literal[
    "deadline",
    "type",
    "agency_excluded",
    "set_aside",
    "eligibility",
    "value",
    "place",
    "negative_keyword",
    # [filters] (§5.2a)
    "naics",
    "psc",
    "agency",
    "set_aside_filter",
    "keyword",
    "posted",
    "due",
    "sole_source",
    "no_deadline",
    "unknown_value",
]

REJECT_REASONS: tuple[RejectReason, ...] = (
    "deadline",
    "type",
    "agency_excluded",
    "set_aside",
    "eligibility",
    "value",
    "place",
    "negative_keyword",
    "naics",
    "psc",
    "agency",
    "set_aside_filter",
    "keyword",
    "posted",
    "due",
    "sole_source",
    "no_deadline",
    "unknown_value",
)

# `[filters] set_asides` entry that keeps notices with no set-aside.
UNRESTRICTED = "NONE"

_SOLE_SOURCE_TITLE_RE = re.compile(
    r"\bsole[\s-]*source\b"
    r"|\bintent\s+to\s+(?:award|sole|negotiate)"
    r"|\bbrand[\s-]+name\s+justification",
    re.I,
)


def _fails_deadline(opp: Opportunity, profile: BusinessProfile, today: date) -> bool:
    if opp.deadline is None:
        return False
    days_left = (opp.deadline.date() - today).days
    return days_left < 0 or days_left < profile.min_days_to_respond


def _fails_notice_type(opp: Opportunity, profile: BusinessProfile) -> bool:
    if opp.kind == "grant":
        return not profile.include_grants
    return opp.notice_type not in profile.notice_types


def _fails_value_range(opp: Opportunity, profile: BusinessProfile) -> bool:
    if opp.value_amount is None:
        return False
    if profile.min_value is not None and opp.value_amount < profile.min_value:
        return True
    return bool(profile.max_value is not None and opp.value_amount > profile.max_value)


def _fails_place(opp: Opportunity, profile: BusinessProfile) -> bool:
    if not profile.states or profile.remote_ok:
        return False
    return bool(opp.place_state and opp.place_state not in profile.states)


def _fails_negative_keywords(opp: Opportunity, profile: BusinessProfile) -> bool:
    text = opp.title
    if profile.filters.negative_keywords_in_description and opp.description_text:
        text = f"{text} {opp.description_text}"
    text_lower = text.lower()
    return any(neg.lower() in text_lower for neg in profile.negative_keywords)


def _has_word(keyword: str, text: str) -> bool:
    return re.search(r"\b" + re.escape(keyword.lower()) + r"\b", text) is not None


def _check_profile_filters(
    opp: Opportunity, profile: BusinessProfile, today: date
) -> RejectReason | None:
    """`[filters]` (§5.2a), in the order of the RejectReason list."""
    f = profile.filters
    contract = opp.kind == "contract"
    if (f.naics and contract and opp.naics
            and not any(n.startswith(p) for n in opp.naics for p in f.naics)):
        return "naics"
    if (f.psc and contract and opp.psc
            and not any(opp.psc.upper().startswith(p.upper()) for p in f.psc)):
        return "psc"
    if f.agencies:
        org = " ".join(x for x in (opp.agency, opp.sub_agency, opp.office) if x).lower()
        if not any(a.lower() in org for a in f.agencies):
            return "agency"
    if f.set_asides and contract:
        code = opp.set_aside_code or UNRESTRICTED
        if code.upper() not in {c.upper() for c in f.set_asides}:
            return "set_aside_filter"
    if f.keywords_any or f.keywords_all:
        text = f"{opp.title} {opp.description_text or ''}".lower()
        if f.keywords_any and not any(_has_word(k, text) for k in f.keywords_any):
            return "keyword"
        if not all(_has_word(k, text) for k in f.keywords_all):
            return "keyword"
    if f.posted_within_days is not None and (
        (today - opp.posted_date).days > f.posted_within_days
    ):
        return "posted"
    if f.due_within_days is not None and opp.deadline is not None and (
        (opp.deadline.date() - today).days > f.due_within_days
    ):
        return "due"
    if f.exclude_sole_source and (
        opp.set_aside_code in SOLE_SOURCE_CODES or _SOLE_SOURCE_TITLE_RE.search(opp.title)
    ):
        return "sole_source"
    if f.require_deadline and opp.deadline is None:
        return "no_deadline"
    if f.require_known_value and opp.value_amount is None:
        return "unknown_value"
    return None


def check_hard_filters(
    opp: Opportunity, profile: BusinessProfile, *, today: date
) -> RejectReason | None:
    """Return the first hard-filter reject reason, or None if `opp` survives all of them."""
    if _fails_deadline(opp, profile, today):
        return "deadline"
    if _fails_notice_type(opp, profile):
        return "type"
    if opp.agency and opp.agency in profile.excluded_agencies:
        return "agency_excluded"
    if not is_eligible_for_set_aside(opp.set_aside_code, profile):
        return "set_aside"
    if not is_eligible_for_grant(opp.eligibility_codes, profile.entity_type):
        return "eligibility"
    if _fails_value_range(opp, profile):
        return "value"
    if _fails_place(opp, profile):
        return "place"
    if _fails_negative_keywords(opp, profile):
        return "negative_keyword"
    return _check_profile_filters(opp, profile, today)


def partition(
    opportunities: list[Opportunity], profile: BusinessProfile, *, today: date
) -> tuple[list[Opportunity], dict[RejectReason, int]]:
    """Split into (survivors, reject-reason counts), preserving input order."""
    survivors: list[Opportunity] = []
    rejected: dict[RejectReason, int] = dict.fromkeys(REJECT_REASONS, 0)
    for opp in opportunities:
        reason = check_hard_filters(opp, profile, today=today)
        if reason is None:
            survivors.append(opp)
        else:
            rejected[reason] += 1
    return survivors, rejected
