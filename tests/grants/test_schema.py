from datetime import UTC, datetime

from agents.grants.models import SubScores
from agents.grants.schema import (
    AllRow,
    FetchedCounts,
    GrantsAll,
    GrantsLatest,
    KeyStat,
    Meta,
    ProfileSummary,
    RejectedCounts,
    SourceLink,
    Stats,
    Thresholds,
    TopMatch,
    ValueBlock,
    cap_all_rows,
)

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def make_top_match(**overrides) -> TopMatch:
    base = dict(
        id="sam:5b345bbb7127b91a3ad577b203fc6f68",
        source="sam",
        kind="contract",
        notice_type="solicitation",
        notice_type_label="Solicitation",
        title="Cloud Modernization Support Services",
        solicitation_number="36C10B26Q0123",
        agency="VETERANS AFFAIRS, DEPARTMENT OF",
        naics=["541512"],
        psc="DA01",
        set_aside_label="Total Small Business Set-Aside",
        posted_date="2026-09-22",
        deadline="2026-10-15T16:00:00-04:00",
        days_left=22,
        place="Remote / Washington, DC",
        value=ValueBlock(kind="none", amount=None, floor=None),
        url="https://sam.gov/opp/5b345bbb7127b91a3ad577b203fc6f68/view",
        fit=86,
        sub_scores=SubScores(capability=36, eligibility=18, size=11, timeline=13, strategic=8),
        recommendation="Pursue",
        confidence="high",
        reasons=["NAICS 541512 primary match", "Python/React modernization scope"],
        red_flags=[],
        is_new=True,
        changed=False,
    )
    base.update(overrides)
    return TopMatch.model_validate(base)


def make_latest(**overrides) -> GrantsLatest:
    base = dict(
        meta=Meta(generated_at=NOW, sam_requests_used=3),
        headline="7 new matches today; 3 close within 14 days.",
        key_stats=[KeyStat(label="New matches today", value=7, format="count")],
        profile=ProfileSummary(
            id="default",
            name="Small software consultancy",
            naics=["541511", "541512"],
            set_asides_eligible=["Total Small Business (SBA)"],
            keywords_preview=["software development", "cloud migration"],
            profile_hash="abc123",
        ),
        thresholds=Thresholds(relevance=25, pursue=75, consider=55),
        stats=Stats(
            new_since_last_run=7,
            closing_within_14d=3,
            active_matches=41,
            fetched=FetchedCounts(sam=912, grants_gov=188),
            rejected=RejectedCounts(deadline=120, type=310),
            below_relevance=402,
            llm_scored_this_run=31,
            llm_scored_cached=118,
        ),
        top_matches=[make_top_match()],
        deadlines_30d=[],
        sources=[
            SourceLink(name="SAM.gov Contract Opportunities", url="https://sam.gov/"),
            SourceLink(name="Grants.gov", url="https://www.grants.gov/"),
        ],
        disclaimer="Automated screening. Always read the official notice first.",
    )
    base.update(overrides)
    return GrantsLatest.model_validate(base)


def test_latest_matches_spec_example_shape():
    latest = make_latest()
    assert latest.meta.sam_requests_used == 3
    assert latest.top_matches[0].fit == 86
    assert latest.stats.rejected.deadline == 120
    assert latest.stats.rejected.negative_keyword == 0  # default


def test_latest_round_trips_through_json():
    latest = make_latest()
    restored = GrantsLatest.model_validate_json(latest.model_dump_json())
    assert restored == latest


def test_latest_rejects_extra_fields():
    import pytest
    from pydantic import ValidationError

    data = make_latest().model_dump(mode="json")
    data["unexpected_field"] = "nope"
    with pytest.raises(ValidationError):
        GrantsLatest.model_validate(data)


def test_all_row_optional_fit_for_below_threshold_items():
    row = AllRow(
        id="sam:xyz",
        source="sam",
        kind="contract",
        type="Sources Sought",
        title="Untitled",
        naics=["999999"],
        posted="2026-09-20",
        relevance=10,
        reasons=[],
        url="https://sam.gov/opp/xyz/view",
        is_new=False,
        in_top=False,
    )
    assert row.fit is None
    assert row.recommendation is None


def test_cap_all_rows_sorts_by_fit_then_relevance():
    def row(id_, fit, relevance):
        return AllRow(
            id=id_,
            source="sam",
            kind="contract",
            type="Solicitation",
            title="x",
            naics=[],
            posted="2026-09-20",
            fit=fit,
            relevance=relevance,
            reasons=[],
            url=f"https://sam.gov/opp/{id_}/view",
            is_new=False,
            in_top=False,
        )

    rows = [
        row("low_fit", 40, 90),
        row("no_fit_high_relevance", None, 80),
        row("high_fit", 90, 50),
        row("no_fit_low_relevance", None, 10),
    ]
    ordered = cap_all_rows(rows, max_rows=10)
    assert [r.id for r in ordered] == [
        "high_fit",
        "low_fit",
        "no_fit_high_relevance",
        "no_fit_low_relevance",
    ]


def test_cap_all_rows_truncates_to_max_rows():
    def row(id_):
        return AllRow(
            id=id_,
            source="sam",
            kind="contract",
            type="Solicitation",
            title="x",
            naics=[],
            posted="2026-09-20",
            relevance=50,
            reasons=[],
            url=f"https://sam.gov/opp/{id_}/view",
            is_new=False,
            in_top=False,
        )

    rows = [row(f"id{i}") for i in range(10)]
    capped = cap_all_rows(rows, max_rows=3)
    assert len(capped) == 3


def test_all_json_round_trips():
    all_json = GrantsAll(
        generated_at=NOW,
        rows=[
            AllRow(
                id="sam:xyz",
                source="sam",
                kind="contract",
                type="Sources Sought",
                title="Untitled",
                naics=["999999"],
                posted="2026-09-20",
                relevance=10,
                reasons=[],
                url="https://sam.gov/opp/xyz/view",
                is_new=False,
                in_top=False,
            )
        ],
    )
    restored = GrantsAll.model_validate_json(all_json.model_dump_json())
    assert restored == all_json
