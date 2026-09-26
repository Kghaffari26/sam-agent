from datetime import UTC, date, datetime

from agents_core.costs import CostTracker
from agents_core.llm import LLM

from agents.grants.config import GrantsConfig
from agents.grants.models import Score, SubScores
from agents.grants.prompting import dates_in, human_date, input_facts, unsupported_dates
from agents.grants.summarize import (
    SUMMARY_PROMPT_VERSION,
    SummaryOutput,
    cache_key_matches,
    make_guard,
    summarize_one,
    summary_input,
)
from agents.grants.templates import headline, summary_fallback
from tests.grants.conftest import make_opportunity, make_profile
from tests.grants.fakes import FakeClient, default_summary

NOW = datetime(2026, 9, 24, tzinfo=UTC)
TODAY = date(2026, 9, 24)


def make_score(**overrides) -> Score:
    base = dict(
        opportunity_id="sam:abc123",
        sub_scores=SubScores(capability=36, eligibility=18, size=11, timeline=13, strategic=8),
        fit=86,
        recommendation="Pursue",
        reasons=["Primary NAICS match", "Modernization scope"],
        red_flags=[],
        confidence="high",
        model_id="m",
        profile_hash="p",
        prompt_version="v1",
        content_hash="deadbeef",
        scored_at=NOW,
    )
    base.update(overrides)
    return Score(**base)


def make_llm(client, tmp_path, max_usd=0.50):
    tracker = CostTracker(agent="grants", run_id="t", max_usd=max_usd,
                          path=tmp_path / "costs.jsonl")
    return LLM(tracker, client=client, sleep=lambda s: None)


def guard_for(opp, score):
    profile = make_profile()
    return make_guard(summary_input(opp, score, profile, TODAY), "PROFILE")


def test_guard_accepts_numbers_and_dates_from_the_input():
    opp = make_opportunity(value_amount=1_200_000, value_kind="award_ceiling")
    guard = guard_for(opp, make_score())
    ok = SummaryOutput(
        what_they_want="VA wants cloud modernization, up to $1.2M, due October 15, 2026.",
        why_fit=["NAICS 541512 is a primary match", "Fit 86"],
        risks=["21 days is a tight turnaround"],
        next_steps=["Read the notice"],
    )
    result = guard(ok)
    assert result.ok, result.unsupported


def test_guard_rejects_invented_numbers_and_dates_and_empty_steps():
    guard = guard_for(make_opportunity(), make_score())
    bad = SummaryOutput(
        what_they_want="Worth $4.5M, due November 3, 2027.",
        why_fit=[],
        risks=[],
        next_steps=[],
    )
    result = guard(bad)
    assert not result.ok
    assert {"$4.5M", "November 3", "2027", "(next_steps is empty)"} <= set(result.unsupported)


def test_date_helpers():
    assert human_date(datetime.fromisoformat("2026-10-15T16:00:00-04:00")) == (
        "October 15, 2026 4:00 PM EDT"
    )
    md, years = dates_in("due 2026-10-15 or Oct. 3rd; FY 2027")
    assert md == {(10, 15), (10, 3)} and years == {2026, 2027}
    assert unsupported_dates("by Sept 30, 2026", "2026-09-30") == []
    assert unsupported_dates("by Sept 29", "2026-09-30") == ["Sept 29"]


def test_input_facts_include_numbers_written_in_text():
    facts = input_facts({"title": "Phase 2 award up to $250K", "days_left": 21})
    assert {2.0, 250.0, 250_000.0, 21.0} <= set(facts)


def test_llm_summary_is_cached_by_content_profile_prompt_model(tmp_path):
    client = FakeClient()
    opp = make_opportunity()
    summary, cacheable = summarize_one(make_llm(client, tmp_path), opp, make_score(),
                                       make_profile(), profile_hash="p", today=TODAY, now=NOW)
    assert cacheable and summary.narrative_source == "llm"
    assert summary.prompt_version == SUMMARY_PROMPT_VERSION
    assert summary.next_steps
    assert cache_key_matches(summary, opp, profile_hash="p", model=summary.model)
    assert not cache_key_matches(summary, opp, profile_hash="q", model=summary.model)
    assert len(client.sync_calls) == 1
    assert "bid/no-bid" in client.sync_calls[0]["system"][0]["text"]


def test_double_guard_failure_uses_template(tmp_path):
    def invents(prompt):
        out = default_summary(prompt)
        out["what_they_want"] = "A $9.99B program."
        return out

    client = FakeClient(summary=invents)
    opp = make_opportunity()
    summary, cacheable = summarize_one(make_llm(client, tmp_path), opp, make_score(),
                                       make_profile(), profile_hash="p", today=TODAY, now=NOW)
    assert summary.narrative_source == "template" and cacheable
    assert len(client.sync_calls) == 2
    assert summary.why_fit == ["Primary NAICS match", "Modernization scope"]
    assert "October 15, 2026" in summary.what_they_want


def test_budget_exhaustion_uses_uncacheable_template(tmp_path):
    client = FakeClient()
    summary, cacheable = summarize_one(make_llm(client, tmp_path, max_usd=0.0001),
                                       make_opportunity(), make_score(), make_profile(),
                                       profile_hash="p", today=TODAY, now=NOW)
    assert summary.narrative_source == "template" and not cacheable
    assert client.sync_calls == []


def test_lists_are_trimmed_to_spec_lengths(tmp_path):
    def long_lists(prompt):
        return {"what_they_want": "Cloud work.", "why_fit": ["a", "b", "c", "d"],
                "risks": ["a", "b", "c", "d"], "next_steps": ["1st", "b", "c", "d", "e"]}

    summary, _ = summarize_one(make_llm(FakeClient(summary=long_lists), tmp_path),
                               make_opportunity(), make_score(), make_profile(),
                               profile_hash="p", today=TODAY, now=NOW)
    assert (len(summary.why_fit), len(summary.risks), len(summary.next_steps)) == (3, 3, 4)


def test_template_fallback_and_headline():
    opp = make_opportunity(agency="VETERANS AFFAIRS, DEPARTMENT OF")
    score = make_score(red_flags=["On-site in Alaska"])
    fb = summary_fallback(opp, score)
    assert fb["risks"] == ["On-site in Alaska"] and fb["next_steps"]
    text = headline(new_matches=7, closing_14d=1, top=(opp, score))
    assert text == ("7 new matches today; 1 closes within 14 days. Top: Cloud Modernization "
                    "Support Services (Veterans Affairs), fit 86.")
    assert headline(new_matches=0, closing_14d=0, top=None) == (
        "0 new matches today; 0 close within 14 days."
    )
    assert GrantsConfig().recommendation.pursue == 75
