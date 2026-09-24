from datetime import date

from agents.grants.config import GrantsConfig
from agents.grants.pipeline import format_report, run_pipeline
from tests.grants.conftest import make_opportunity

TODAY = date(2026, 9, 24)


def test_run_pipeline_no_llm_calls_and_reports_reject_reasons(default_profile):
    config = GrantsConfig()
    opportunities = [
        make_opportunity(id="a", source_id="a", naics=["541512"], title="Cloud API modernization"),
        make_opportunity(id="b", source_id="b", title="Janitorial Services"),
        make_opportunity(id="c", source_id="c", deadline="2026-09-01T16:00:00-04:00"),
    ]

    report = run_pipeline(opportunities, default_profile, config, today=TODAY)

    assert report.fetched_count == 3
    assert report.deduped_count == 3
    assert report.survivor_count == 1
    assert report.rejected["negative_keyword"] == 1
    assert report.rejected["deadline"] == 1
    assert [opp.id for opp, _ in report.candidates] == ["a"]
    assert report.below_threshold_count == 0


def test_run_pipeline_dedupes_before_filtering(default_profile):
    config = GrantsConfig()
    older = make_opportunity(id="sam:1", source_id="1", posted_date="2026-09-01")
    newer = make_opportunity(id="sam:1", source_id="1", posted_date="2026-09-10")

    report = run_pipeline([older, newer], default_profile, config, today=TODAY)

    assert report.fetched_count == 2
    assert report.deduped_count == 1


def test_format_report_includes_reject_table_and_top_candidates(default_profile):
    config = GrantsConfig()
    opportunities = [
        make_opportunity(id="a", source_id="a", naics=["541512"], title="Cloud API modernization"),
        make_opportunity(id="b", source_id="b", title="Janitorial Services"),
    ]
    report = run_pipeline(opportunities, default_profile, config, today=TODAY)
    text = format_report(report, top_n=5)

    assert "Reject reasons:" in text
    assert "negative_keyword" in text
    assert "Top 5 by relevance" in text
    assert "a" in text  # candidate id substring present
