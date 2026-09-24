from datetime import date

from agents.grants.filters import check_hard_filters, partition
from tests.grants.conftest import make_opportunity, make_profile

TODAY = date(2026, 9, 24)


def test_passes_all_filters_by_default(default_profile):
    opp = make_opportunity()
    assert check_hard_filters(opp, default_profile, today=TODAY) is None


def test_rejects_past_deadline(default_profile):
    opp = make_opportunity(deadline="2026-09-01T16:00:00-04:00")
    assert check_hard_filters(opp, default_profile, today=TODAY) == "deadline"


def test_rejects_deadline_too_soon(default_profile):
    # min_days_to_respond=3; deadline is only 1 day out.
    opp = make_opportunity(deadline="2026-09-25T16:00:00-04:00")
    assert check_hard_filters(opp, default_profile, today=TODAY) == "deadline"


def test_no_deadline_does_not_trigger_deadline_filter(default_profile):
    opp = make_opportunity(deadline=None, notice_type="sources_sought")
    assert check_hard_filters(opp, default_profile, today=TODAY) is None


def test_rejects_disallowed_notice_type(default_profile):
    opp = make_opportunity(notice_type="special_notice")
    assert check_hard_filters(opp, default_profile, today=TODAY) == "type"


def test_grants_allowed_when_include_grants_true(default_profile):
    opp = make_opportunity(
        source="grants_gov",
        kind="grant",
        notice_type="grant_posted",
        id="gg:1",
        source_id="1",
        naics=[],
        url="https://www.grants.gov/search-results-detail/1",
    )
    assert check_hard_filters(opp, default_profile, today=TODAY) is None


def test_grants_rejected_when_include_grants_false(default_profile):
    profile = make_profile(include_grants=False)
    opp = make_opportunity(
        source="grants_gov",
        kind="grant",
        notice_type="grant_posted",
        id="gg:1",
        source_id="1",
        naics=[],
        url="https://www.grants.gov/search-results-detail/1",
    )
    assert check_hard_filters(opp, profile, today=TODAY) == "type"


def test_rejects_excluded_agency(default_profile):
    profile = make_profile(excluded_agencies=["DEPARTMENT OF DEFENSE"])
    opp = make_opportunity(agency="DEPARTMENT OF DEFENSE")
    assert check_hard_filters(opp, profile, today=TODAY) == "agency_excluded"


def test_rejects_set_aside_without_certification(default_profile):
    opp = make_opportunity(set_aside_code="8A")
    assert check_hard_filters(opp, default_profile, today=TODAY) == "set_aside"


def test_accepts_set_aside_with_certification():
    profile = make_profile(certifications=["8A"])
    opp = make_opportunity(set_aside_code="8A")
    assert check_hard_filters(opp, profile, today=TODAY) is None


def test_sba_set_aside_requires_small_business_entity_type():
    profile = make_profile(entity_type="large_business")
    opp = make_opportunity(set_aside_code="SBA")
    assert check_hard_filters(opp, profile, today=TODAY) == "set_aside"


def test_rejects_grant_ineligible_entity_type(default_profile):
    opp = make_opportunity(
        source="grants_gov",
        kind="grant",
        notice_type="grant_posted",
        id="gg:1",
        source_id="1",
        naics=[],
        eligibility_codes=["22"],  # for-profit, not small business
        url="https://www.grants.gov/search-results-detail/1",
    )
    assert check_hard_filters(opp, default_profile, today=TODAY) == "eligibility"


def test_rejects_value_below_min(default_profile):
    opp = make_opportunity(value_amount=1000, value_kind="award_ceiling")
    assert check_hard_filters(opp, default_profile, today=TODAY) == "value"


def test_rejects_value_above_max(default_profile):
    opp = make_opportunity(value_amount=10_000_000, value_kind="award_ceiling")
    assert check_hard_filters(opp, default_profile, today=TODAY) == "value"


def test_unknown_value_is_not_rejected(default_profile):
    opp = make_opportunity(value_amount=None)
    assert check_hard_filters(opp, default_profile, today=TODAY) is None


def test_rejects_place_outside_states_when_not_remote_ok():
    profile = make_profile(states=["CA"], remote_ok=False)
    opp = make_opportunity(place_state="DC")
    assert check_hard_filters(opp, profile, today=TODAY) == "place"


def test_place_filter_skipped_when_remote_ok():
    profile = make_profile(states=["CA"], remote_ok=True)
    opp = make_opportunity(place_state="DC")
    assert check_hard_filters(opp, profile, today=TODAY) is None


def test_place_filter_skipped_when_states_empty(default_profile):
    opp = make_opportunity(place_state="DC")
    assert check_hard_filters(opp, default_profile, today=TODAY) is None


def test_rejects_negative_keyword_in_title(default_profile):
    opp = make_opportunity(title="Facility Janitorial Services Contract")
    assert check_hard_filters(opp, default_profile, today=TODAY) == "negative_keyword"


def test_partition_counts_reject_reasons(default_profile):
    survivors, rejected = partition(
        [
            make_opportunity(id="a", title="Cloud API Modernization"),
            make_opportunity(id="b", title="Janitorial Services"),
            make_opportunity(id="c", deadline="2026-09-01T16:00:00-04:00"),
        ],
        default_profile,
        today=TODAY,
    )
    assert [o.id for o in survivors] == ["a"]
    assert rejected["negative_keyword"] == 1
    assert rejected["deadline"] == 1
    assert rejected["type"] == 0
