from datetime import datetime, timezone

from agents.grants.models import Opportunity, Score, SubScores, Summary


def make_opportunity(**overrides) -> Opportunity:
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    base = dict(
        id="sam:abc123",
        source="sam",
        source_id="abc123",
        kind="contract",
        notice_type="solicitation",
        title="Cloud Modernization Support Services",
        posted_date="2026-09-22",
        url="https://sam.gov/opp/abc123/view",
        content_hash="deadbeef",
        first_seen_at=now,
        last_seen_at=now,
    )
    base.update(overrides)
    return Opportunity.model_validate(base)


def test_opportunity_round_trips_through_json():
    opp = make_opportunity()
    restored = Opportunity.model_validate_json(opp.model_dump_json())
    assert restored == opp


def test_opportunity_defaults():
    opp = make_opportunity()
    assert opp.naics == []
    assert opp.description_fetched is False
    assert opp.value_kind == "none"


def test_score_sums_and_recommendation_are_independent_fields():
    sub_scores = SubScores(capability=36, eligibility=18, size=11, timeline=13, strategic=8)
    score = Score(
        opportunity_id="sam:abc123",
        sub_scores=sub_scores,
        fit=sum(sub_scores.model_dump().values()),
        recommendation="Pursue",
        reasons=["NAICS 541512 primary match"],
        red_flags=[],
        confidence="high",
        model_id="claude-sonnet-5",
        profile_hash="hash123",
        prompt_version="v1",
        content_hash="deadbeef",
        scored_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
    )
    assert score.fit == 86
    assert score.recommendation == "Pursue"


def test_summary_round_trips():
    summary = Summary(
        opportunity_id="sam:abc123",
        what_they_want="They need cloud modernization support.",
        why_fit=["Strong NAICS match"],
        risks=["Tight timeline"],
        next_steps=["Confirm SAM registration is active"],
        narrative_source="llm",
        model="claude-sonnet-5",
        profile_hash="hash123",
        prompt_version="v1",
        content_hash="deadbeef",
        generated_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
    )
    restored = Summary.model_validate_json(summary.model_dump_json())
    assert restored == summary
