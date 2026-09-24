"""Grants.gov applicant-eligibility codes: labels and entity-type matching.

See docs/specs/SPEC_GRANTS.md §3.2 and hard filter #5 in §5.2.

The code list below is taken from the Grants.gov `search2` / `fetchOpportunity`
API guide's "Applicant Types" reference. VERIFY it against the current API guide
before relying on it in production, per the spec's warning in §3.2 -- Grants.gov
has changed this list before.
"""

from __future__ import annotations

GRANTS_GOV_APPLICANT_TYPES: dict[str, str] = {
    "00": "State governments",
    "01": "County governments",
    "02": "City or township governments",
    "04": "Special district governments",
    "05": "Independent school districts",
    "06": "Public and State controlled institutions of higher education",
    "07": "Native American tribal governments (Federally recognized)",
    "08": "Public housing authorities/Indian housing authorities",
    "11": "Native American tribal organizations (other than Federally recognized)",
    "12": "Nonprofits having a 501(c)(3) status with the IRS",
    "13": "Nonprofits without 501(c)(3) status with the IRS, other than institutions of higher education",
    "20": "Private institutions of higher education",
    "21": "Individuals",
    "22": "For-profit organizations other than small businesses",
    "23": "Small businesses",
    "25": "Others",
    "99": "Unrestricted",
}

# Which Grants.gov applicant-type codes satisfy each business_profile.entity_type.
_ENTITY_TYPE_TO_CODES: dict[str, frozenset[str]] = {
    "small_business": frozenset({"23"}),
    "nonprofit": frozenset({"12", "13"}),
    "university": frozenset({"06", "20"}),
    "individual": frozenset({"21"}),
    "large_business": frozenset({"22"}),
}

# Any opportunity carrying this code is open to everyone.
UNRESTRICTED_CODE = "99"


def label_for(code: str) -> str | None:
    return GRANTS_GOV_APPLICANT_TYPES.get(code)


def is_eligible_for_grant(eligibility_codes: list[str], entity_type: str) -> bool:
    """Hard filter #5 (SPEC_GRANTS.md §5.2).

    No listed codes, or the unrestricted code, means everyone is eligible.
    Otherwise the profile's entity_type must map to at least one listed code.
    """
    if not eligibility_codes:
        return True
    if UNRESTRICTED_CODE in eligibility_codes:
        return True

    allowed = _ENTITY_TYPE_TO_CODES.get(entity_type, frozenset())
    return not allowed.isdisjoint(eligibility_codes)
