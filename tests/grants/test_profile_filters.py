"""`[filters]` (SPEC_GRANTS.md §5.2a) and `[scoring]` (§5.4a) in the business profile."""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from agents.grants.config import (
    GrantsConfig,
    ProfileFilters,
    ScoringPreferences,
    effective_thresholds,
    profile_hash,
)
from agents.grants.filters import REJECT_REASONS, check_hard_filters, partition
from agents.grants.schema import RejectedCounts
from agents.grants.scoring import RubricOutput, finalize, refinalize
from tests.grants.conftest import make_opportunity, make_profile

TODAY = date(2026, 9, 24)
NOW = datetime(2026, 9, 24, tzinfo=UTC)


def with_filters(**kw):
    return make_profile(filters=ProfileFilters(**kw))


def reason(opp, **filters):
    return check_hard_filters(opp, with_filters(**filters), today=TODAY)


def grant(**kw):
    base = dict(source="grants_gov", kind="grant", notice_type="grant_posted", id="gg:1",
                source_id="1", naics=[], psc=None,
                url="https://www.grants.gov/search-results-detail/1")
    base.update(kw)
    return make_opportunity(**base)


def test_no_filters_by_default():
    assert ProfileFilters() == make_profile().filters
    assert check_hard_filters(make_opportunity(), make_profile(), today=TODAY) is None


def test_every_reject_reason_is_published():
    assert set(REJECT_REASONS) == set(RejectedCounts.model_fields)


@pytest.mark.parametrize(("naics", "expected"), [
    (["5415"], None), (["541512"], None), (["2382"], "naics"), (["54"], None),
])
def test_naics_filter_matches_codes_and_prefixes(naics, expected):
    assert reason(make_opportunity(naics=["541512"]), naics=naics) == expected


def test_naics_and_psc_filters_skip_grants_and_unknown_codes():
    assert reason(grant(), naics=["2382"], psc=["Z2"]) is None
    assert reason(make_opportunity(naics=[], psc=None), naics=["2382"], psc=["Z2"]) is None


def test_psc_filter():
    assert reason(make_opportunity(psc="DA01"), psc=["da"]) is None
    assert reason(make_opportunity(psc="DA01"), psc=["Z2"]) == "psc"


def test_agency_filter_is_a_substring_over_agency_sub_agency_and_office():
    opp = make_opportunity(agency="VETERANS AFFAIRS, DEPARTMENT OF",
                           sub_agency="VETERANS HEALTH ADMINISTRATION", office="NETWORK 8")
    assert reason(opp, agencies=["veterans health"]) is None
    assert reason(opp, agencies=["network 8"]) is None
    assert reason(opp, agencies=["DEFENSE"]) == "agency"


def test_set_aside_filter_and_unrestricted():
    sba = make_opportunity(set_aside_code="SBA")
    open_ = make_opportunity(set_aside_code=None)
    assert reason(sba, set_asides=["SBA"]) is None
    assert reason(open_, set_asides=["SBA"]) == "set_aside_filter"
    assert reason(open_, set_asides=["SBA", "NONE"]) is None
    assert reason(grant(), set_asides=["SBA"]) is None


def test_keyword_filters_search_title_and_description():
    opp = make_opportunity(title="Cloud Modernization", description_text="Kubernetes and AWS")
    assert reason(opp, keywords_any=["aws", "azure"]) is None
    assert reason(opp, keywords_any=["azure"]) == "keyword"
    assert reason(opp, keywords_all=["cloud", "aws"]) is None
    assert reason(opp, keywords_all=["cloud", "azure"]) == "keyword"
    assert reason(opp, keywords_any=["moderniz"]) == "keyword"  # whole words only


def test_posted_and_due_windows():
    opp = make_opportunity(posted_date="2026-09-01", deadline="2026-11-30T16:00:00-05:00")
    assert reason(opp, posted_within_days=30) is None
    assert reason(opp, posted_within_days=7) == "posted"
    assert reason(opp, due_within_days=90) is None
    assert reason(opp, due_within_days=30) == "due"
    assert reason(make_opportunity(deadline=None, notice_type="sources_sought"),
                  due_within_days=30) is None


@pytest.mark.parametrize(("title", "code", "expected"), [
    ("Notice of Intent to Sole Source: Widget support", None, "sole_source"),
    ("Intent to Award a Contract to Acme", None, "sole_source"),
    ("Sole-Source Justification", None, "sole_source"),
    ("Cloud support", "8A", "sole_source"),
    ("Cloud support", "SDVOSBC", "sole_source"),
    ("Cloud support", "SBA", None),
    ("Resource planning", None, None),
])
def test_sole_source_exclusion(title, code, expected):
    profile = make_profile(certifications=["8A", "SDVOSB"],
                           filters=ProfileFilters(exclude_sole_source=True))
    opp = make_opportunity(title=title, set_aside_code=code)
    assert check_hard_filters(opp, profile, today=TODAY) == expected


def test_sole_source_kept_when_filter_off():
    opp = make_opportunity(title="Notice of Intent to Sole Source")
    assert check_hard_filters(opp, make_profile(), today=TODAY) is None


def test_negative_keywords_in_description():
    opp = make_opportunity(description_text="Includes janitorial services for the building")
    assert check_hard_filters(opp, make_profile(), today=TODAY) is None
    assert reason(opp, negative_keywords_in_description=True) == "negative_keyword"


def test_require_deadline_and_known_value():
    forecast = make_opportunity(deadline=None, notice_type="sources_sought")
    assert reason(forecast, require_deadline=True) == "no_deadline"
    assert reason(make_opportunity(), require_known_value=True) == "unknown_value"
    valued = make_opportunity(value_kind="estimated_total", value_amount=100000)
    assert reason(valued, require_known_value=True) is None


def test_partition_counts_new_reasons():
    profile = with_filters(naics=["2382"])
    survivors, rejected = partition([make_opportunity()], profile, today=TODAY)
    assert survivors == [] and rejected["naics"] == 1


def test_profile_hash_ignores_filters_and_scoring():
    base = make_profile()
    tuned = make_profile(
        filters=ProfileFilters(naics=["5415"], exclude_sole_source=True),
        scoring=ScoringPreferences(pursue=80, consider=60, set_aside_bonus=5),
    )
    assert profile_hash(base) == profile_hash(tuned)
    assert profile_hash(base) != profile_hash(make_profile(keywords=["other"]))


# ---- [scoring] ------------------------------------------------------------------


def rubric(**kw):
    base = dict(capability=30, eligibility=15, size=8, timeline=10, strategic=5,
                reasons=["Good match"], red_flags=[], confidence="high")
    base.update(kw)
    return RubricOutput(**base)


def fin(raw, profile, opp=None, config=None):
    return finalize(raw, opp or make_opportunity(description_fetched=True), profile,
                    config or GrantsConfig(), profile_hash="p", model="m", now=NOW)


def test_default_weights_keep_the_plain_sum():
    assert fin(rubric(), make_profile()).fit == 30 + 15 + 8 + 10 + 5


def test_weights_rescale_each_dimension():
    weights = {"capability": 60, "eligibility": 10, "size": 10, "timeline": 10, "strategic": 10}
    profile = make_profile(scoring=ScoringPreferences(weights=weights))
    # 30/40*60 + 15/20*10 + 8/15*10 + 10/15*10 + 5/10*10 = 45 + 7.5 + 5.33 + 6.67 + 5
    assert fin(rubric(), profile).fit == 70


def test_set_aside_bonus_only_for_held_status():
    profile = make_profile(certifications=["SDVOSB"],
                           scoring=ScoringPreferences(set_aside_bonus=5))
    held = make_opportunity(description_fetched=True, set_aside_code="SDVOSBS")
    not_held = make_opportunity(description_fetched=True, set_aside_code="WOSB")
    assert fin(rubric(), profile, held).fit == 68 + 5
    assert fin(rubric(), profile, not_held).fit == 68
    full = rubric(capability=40, eligibility=20, size=15, timeline=15, strategic=10)
    assert fin(full, profile, held).fit == 100


def test_profile_bands_override_config():
    profile = make_profile(scoring=ScoringPreferences(pursue=80, consider=60))
    assert effective_thresholds(profile, GrantsConfig()) == (80, 60)
    assert effective_thresholds(make_profile(), GrantsConfig()) == (75, 55)
    assert fin(rubric(capability=37), profile).recommendation == "Consider"  # fit 75
    assert fin(rubric(capability=37), make_profile()).recommendation == "Pursue"


def test_hard_cap_still_applies_after_weights_and_bonus():
    profile = make_profile(scoring=ScoringPreferences(set_aside_bonus=10))
    opp = make_opportunity(description_fetched=True, set_aside_code="SBA")
    score = fin(rubric(red_flags=["Requires Top Secret clearance"]), profile, opp)
    assert score.fit == 20 and score.capped


def test_refinalize_applies_new_scoring_without_an_llm_call():
    opp = make_opportunity(description_fetched=True)
    cached = fin(rubric(), make_profile(), opp)
    weights = {"capability": 60, "eligibility": 10, "size": 10, "timeline": 10, "strategic": 10}
    profile = make_profile(scoring=ScoringPreferences(weights=weights))
    assert refinalize(cached, opp, profile, GrantsConfig()).fit == 70


@pytest.mark.parametrize("kw", [
    {"weights": {"capability": 50, "eligibility": 20, "size": 15, "timeline": 15,
                 "strategic": 10}},
    {"weights": {"capability": 90, "eligibility": 10}},
    {"pursue": 50, "consider": 60},
    {"set_aside_bonus": 30},
])
def test_invalid_scoring_rejected(kw):
    with pytest.raises(ValidationError):
        ScoringPreferences(**kw)
