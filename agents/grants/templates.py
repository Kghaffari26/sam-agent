"""Deterministic text: the summary fallback (§7.4) and the run headline (§6.1).

Both are built only from computed data, never call the LLM, and never raise.
"""

from __future__ import annotations

from agents.grants.models import Opportunity, Score
from agents.grants.prompting import human_date

HEADLINE_TITLE_CHARS = 80

STANDARD_NEXT_STEPS_CONTRACT = [
    "Read the full notice and every attachment on SAM.gov",
    "Confirm your SAM.gov registration is active",
    "Submit questions before the Q&A deadline",
    "Prepare a capability statement tailored to the scope",
]
STANDARD_NEXT_STEPS_GRANT = [
    "Read the full funding opportunity on Grants.gov",
    "Confirm eligibility and Grants.gov/SAM.gov registration",
    "Outline the project narrative against the review criteria",
    "Plan the budget and any cost-sharing requirement",
]


def summary_fallback(opp: Opportunity, score: Score) -> dict[str, list[str] | str]:
    """§7.4: title, agency, deadline and the top 3 reasons."""
    who = opp.agency or "The agency"
    deadline = f" Responses are due {human_date(opp.deadline)}." if opp.deadline else ""
    what = f"{who} posted: {opp.title}.{deadline}"
    risks = score.red_flags[:3] or [
        "Scope and requirements are unverified until the full notice is read"
    ]
    steps = STANDARD_NEXT_STEPS_GRANT if opp.kind == "grant" else STANDARD_NEXT_STEPS_CONTRACT
    return {
        "what_they_want": what,
        "why_fit": score.reasons[:3] or ["Matches the profile's keywords"],
        "risks": risks,
        "next_steps": list(steps),
    }


def _short_agency(agency: str | None) -> str | None:
    if not agency:
        return None
    # "VETERANS AFFAIRS, DEPARTMENT OF" -> "Veterans Affairs"
    head = agency.split(",")[0].strip()
    return head.title() if head.isupper() else head


def headline(
    *, new_matches: int, closing_14d: int, top: tuple[Opportunity, Score] | None
) -> str:
    match_word = "match" if new_matches == 1 else "matches"
    close_word = "closes" if closing_14d == 1 else "close"
    text = f"{new_matches} new {match_word} today; {closing_14d} {close_word} within 14 days."
    if top is not None:
        opp, score = top
        agency = _short_agency(opp.agency)
        where = f" ({agency})" if agency else ""
        title = opp.title if len(opp.title) <= HEADLINE_TITLE_CHARS else (
            opp.title[: HEADLINE_TITLE_CHARS - 1].rstrip() + "…"
        )
        text += f" Top: {title}{where}, fit {score.fit}."
    return text
