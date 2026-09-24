from agents.grants.relevance import compute_relevance, select_candidates
from tests.grants.conftest import make_opportunity, make_profile


def test_naics_primary_match(default_profile):
    opp = make_opportunity(naics=["541512"], psc=None, title="Widget procurement")
    assert compute_relevance(opp, default_profile) == 50


def test_naics_secondary_match(default_profile):
    opp = make_opportunity(naics=["541519"], psc=None, title="Widget procurement")
    assert compute_relevance(opp, default_profile) == 35


def test_naics_prefix_match_only_when_no_direct_hit(default_profile):
    # 5415xx shares the 4-digit prefix with primary 541511/541512, but isn't
    # itself in naics_primary or naics_secondary.
    opp = make_opportunity(naics=["541513"], psc=None, title="Widget procurement")
    assert compute_relevance(opp, default_profile) == 20


def test_naics_prefix_not_double_counted_with_primary(default_profile):
    opp = make_opportunity(naics=["541512", "541513"], psc=None, title="Widget procurement")
    assert compute_relevance(opp, default_profile) == 50


def test_no_naics_match_scores_zero_for_that_signal(default_profile):
    opp = make_opportunity(naics=["999999"], psc=None, title="Widget procurement")
    assert compute_relevance(opp, default_profile) == 0


def test_psc_prefix_match(default_profile):
    opp = make_opportunity(naics=[], psc="DA01", title="Widget procurement")
    assert compute_relevance(opp, default_profile) == 15


def test_title_keywords_capped_at_30(default_profile):
    opp = make_opportunity(
        naics=[],
        psc=None,
        title="Cloud software development modernization project",
    )
    # "cloud", "software development", "modernization" = 3 hits * 10 = 30 (at cap)
    assert compute_relevance(opp, default_profile) == 30


def test_description_keywords_capped_at_20():
    # 5 keyword hits * 5 points = 25, capped at 20.
    profile = make_profile(keywords=["cloud", "api", "dashboard", "modernization", "pipeline"])
    opp = make_opportunity(
        naics=[],
        psc=None,
        title="Untitled procurement",
        description_text="A cloud api dashboard modernization data pipeline effort.",
    )
    assert compute_relevance(opp, profile) == 20


def test_target_agency_bonus(default_profile):
    opp = make_opportunity(
        naics=[], psc=None, title="Untitled", agency="GENERAL SERVICES ADMINISTRATION"
    )
    assert compute_relevance(opp, default_profile) == 5


def test_negative_keyword_in_description_penalized(default_profile):
    opp = make_opportunity(
        naics=["541512"],
        psc=None,
        title="Untitled",
        description_text="This includes janitorial services as a minor component.",
    )
    # NAICS primary (50) - negative keyword penalty (30) = 20
    assert compute_relevance(opp, default_profile) == 20


def test_score_clamped_to_zero():
    profile = make_profile(negative_keywords=["bad"] * 1, naics_primary=[], naics_secondary=[])
    opp = make_opportunity(
        naics=[], psc=None, title="Untitled", description_text="This is a bad opportunity."
    )
    assert compute_relevance(opp, profile) == 0


def test_grant_without_naics_relies_on_keywords(default_profile):
    opp = make_opportunity(
        source="grants_gov",
        kind="grant",
        notice_type="grant_posted",
        id="gg:1",
        source_id="1",
        naics=[],
        psc=None,
        title="Cloud software development grant",
        url="https://www.grants.gov/search-results-detail/1",
    )
    assert compute_relevance(opp, default_profile) == 20  # "cloud" + "software development"


def test_select_candidates_sorts_and_caps(default_profile):
    opps = [
        make_opportunity(id="low", naics=[], psc=None, title="Untitled"),  # 0
        make_opportunity(id="mid", naics=["541519"], psc=None, title="Untitled"),  # 35
        make_opportunity(id="high", naics=["541512"], psc=None, title="Untitled"),  # 50
    ]
    candidates, below = select_candidates(
        opps, default_profile, relevance_threshold=25, max_candidates=1
    )
    assert [opp.id for opp, _ in candidates] == ["high"]
    below_ids = {opp.id for opp, _ in below}
    assert below_ids == {"low", "mid"}


def test_select_candidates_respects_threshold(default_profile):
    opps = [
        make_opportunity(id="above", naics=["541512"], psc=None, title="Untitled"),  # 50
        make_opportunity(id="below", naics=[], psc=None, title="Untitled"),  # 0
    ]
    candidates, below = select_candidates(
        opps, default_profile, relevance_threshold=25, max_candidates=150
    )
    assert [opp.id for opp, _ in candidates] == ["above"]
    assert [opp.id for opp, _ in below] == ["below"]
