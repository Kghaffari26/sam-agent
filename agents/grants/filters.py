"""Deterministic hard filters, applied before any LLM call (SPEC_GRANTS.md §5.2).

Rules run in order; the first one an opportunity fails is its reject reason.
Reject counts by reason code are published for transparency (§6, `stats.rejected`).
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from agents.grants.config import BusinessProfile
from agents.grants.eligibility import is_eligible_for_grant
from agents.grants.models import Opportunity
from agents.grants.setasides import is_eligible_for_set_aside

RejectReason = Literal[
    "deadline",
    "type",
    "agency_excluded",
    "set_aside",
    "eligibility",
    "value",
    "place",
    "negative_keyword",
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
    title_lower = opp.title.lower()
    return any(neg.lower() in title_lower for neg in profile.negative_keywords)


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
    return None


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
