"""SAM.gov set-aside codes: labels and eligibility rules.

See docs/specs/SPEC_GRANTS.md §3.1 and hard filter #4 in §5.2.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.grants.config import BusinessProfile


@dataclass(frozen=True)
class SetAsideInfo:
    code: str
    label: str
    # Certification the profile must hold in `certifications` to be eligible.
    # None means eligibility is driven by rules other than a named certification
    # (e.g. small-business status, or a status this agent can't verify locally).
    required_certification: str | None


# Codes and labels per SPEC_GRANTS.md §3.1. `required_certification` values line up
# with the example `certifications` list in config/business_profile.toml.
SET_ASIDES: dict[str, SetAsideInfo] = {
    "SBA": SetAsideInfo("SBA", "Total Small Business Set-Aside", None),
    "SBP": SetAsideInfo("SBP", "Partial Small Business Set-Aside", None),
    "8A": SetAsideInfo("8A", "8(a) Sole Source", "8A"),
    "8AN": SetAsideInfo("8AN", "8(a) Set-Aside", "8A"),
    "HZC": SetAsideInfo("HZC", "HUBZone Sole Source", "HUBZone"),
    "HZS": SetAsideInfo("HZS", "HUBZone Set-Aside", "HUBZone"),
    "SDVOSBC": SetAsideInfo(
        "SDVOSBC", "Service-Disabled Veteran-Owned Small Business Sole Source", "SDVOSB"
    ),
    "SDVOSBS": SetAsideInfo(
        "SDVOSBS", "Service-Disabled Veteran-Owned Small Business Set-Aside", "SDVOSB"
    ),
    "WOSB": SetAsideInfo("WOSB", "Women-Owned Small Business Set-Aside", "WOSB"),
    "WOSBSS": SetAsideInfo("WOSBSS", "Women-Owned Small Business Sole Source", "WOSB"),
    "EDWOSB": SetAsideInfo(
        "EDWOSB", "Economically Disadvantaged Women-Owned Small Business Set-Aside", "EDWOSB"
    ),
    "EDWOSBSS": SetAsideInfo(
        "EDWOSBSS", "Economically Disadvantaged Women-Owned Small Business Sole Source", "EDWOSB"
    ),
    "VSA": SetAsideInfo("VSA", "Veteran-Owned Small Business Set-Aside", "VOSB"),
    "VSS": SetAsideInfo("VSS", "Veteran-Owned Small Business Sole Source", "VOSB"),
    "IEE": SetAsideInfo("IEE", "Indian Economic Enterprise Set-Aside", None),
    "ISBEE": SetAsideInfo("ISBEE", "Indian Small Business Economic Enterprise Set-Aside", None),
    "BICiv": SetAsideInfo("BICiv", "Buy Indian Set-Aside (Civilian)", None),
    "LAS": SetAsideInfo("LAS", "Local Area Set-Aside", None),
}

# Codes whose eligibility is small-business status rather than a named certification.
_SMALL_BUSINESS_STATUS_CODES = {"SBA", "SBP"}


def get_set_aside(code: str | None) -> SetAsideInfo | None:
    if not code:
        return None
    return SET_ASIDES.get(code)


def is_eligible_for_set_aside(code: str | None, profile: BusinessProfile) -> bool:
    """Hard filter #4 (SPEC_GRANTS.md §5.2): does the profile qualify for this set-aside?

    An unrecognized code is not treated as a blocker: it's logged upstream and left
    to the LLM rubric / human review rather than silently dropped.
    """
    if not code:
        return True

    info = SET_ASIDES.get(code)
    if info is None:
        return True

    if code in _SMALL_BUSINESS_STATUS_CODES:
        return profile.entity_type == "small_business"

    if info.required_certification is None:
        return True

    return info.required_certification in profile.certifications


def label_for(code: str | None) -> str | None:
    info = get_set_aside(code)
    return info.label if info else None
