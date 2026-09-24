from datetime import UTC, date, datetime

from agents.grants.store import (
    dedupe_across_sources,
    dedupe_within_source,
    is_expired,
    merge_into_store,
    normalize_title,
    prune_store,
)
from tests.grants.conftest import make_opportunity

NOW = datetime(2026, 9, 24, tzinfo=UTC)
LATER = datetime(2026, 9, 25, tzinfo=UTC)


def test_normalize_title_lowercases_and_strips_punctuation():
    assert normalize_title("Cloud-Modernization: Phase II!") == "cloud modernization phase ii"


# --- dedupe_within_source ----------------------------------------------------


def test_dedupe_within_source_keeps_latest_posted_date():
    older = make_opportunity(id="sam:1", source_id="1", posted_date="2026-09-01")
    newer = make_opportunity(id="sam:1", source_id="1", posted_date="2026-09-10")
    result = dedupe_within_source([older, newer])
    assert len(result) == 1
    assert result[0].posted_date == date(2026, 9, 10)


def test_dedupe_within_source_collapses_sam_amendments_by_solicitation_number():
    original = make_opportunity(
        id="sam:1", source_id="1", solicitation_number="ABC123", posted_date="2026-09-01"
    )
    amendment = make_opportunity(
        id="sam:2", source_id="2", solicitation_number="ABC123", posted_date="2026-09-10"
    )
    result = dedupe_within_source([original, amendment])
    assert len(result) == 1
    assert result[0].id == "sam:2"


def test_dedupe_within_source_keeps_distinct_solicitations():
    a = make_opportunity(
        id="sam:1", source_id="1", solicitation_number="A", posted_date="2026-09-01"
    )
    b = make_opportunity(
        id="sam:2", source_id="2", solicitation_number="B", posted_date="2026-09-01"
    )
    result = dedupe_within_source([a, b])
    assert {opp.id for opp in result} == {"sam:1", "sam:2"}


# --- dedupe_across_sources ----------------------------------------------------


def test_dedupe_across_sources_merges_similar_title_and_matching_agency():
    sam_opp = make_opportunity(
        id="sam:1",
        source="sam",
        source_id="1",
        title="Cloud Modernization Support Services",
        agency="GENERAL SERVICES ADMINISTRATION",
        posted_date="2026-09-01",
    )
    grant_opp = make_opportunity(
        id="gg:1",
        source="grants_gov",
        source_id="1",
        kind="grant",
        notice_type="grant_posted",
        title="Cloud Modernization Support Services!",
        agency="GENERAL SERVICES ADMINISTRATION",
        posted_date="2026-09-10",
        naics=[],
        url="https://www.grants.gov/search-results-detail/1",
    )
    result = dedupe_across_sources([sam_opp, grant_opp])
    assert len(result) == 1
    assert result[0].id == "gg:1"  # the more recently posted one is kept


def test_dedupe_across_sources_does_not_merge_different_agency():
    sam_opp = make_opportunity(
        id="sam:1", title="Cloud Modernization Support Services", agency="GSA"
    )
    grant_opp = make_opportunity(
        id="gg:1",
        source="grants_gov",
        source_id="1",
        kind="grant",
        notice_type="grant_posted",
        title="Cloud Modernization Support Services",
        agency="DOE",
        naics=[],
        url="https://www.grants.gov/search-results-detail/1",
    )
    result = dedupe_across_sources([sam_opp, grant_opp])
    assert len(result) == 2


def test_dedupe_across_sources_does_not_merge_same_source():
    a = make_opportunity(id="sam:1", source_id="1", title="Cloud Modernization", agency="GSA")
    b = make_opportunity(id="sam:2", source_id="2", title="Cloud Modernization", agency="GSA")
    result = dedupe_across_sources([a, b])
    assert len(result) == 2


# --- is_expired ----------------------------------------------------------------


def test_is_expired_past_deadline():
    opp = make_opportunity(deadline="2026-09-01T16:00:00-04:00")
    assert is_expired(opp, today=date(2026, 9, 24), forecast_max_age_days=180) is True


def test_is_expired_future_deadline_not_expired():
    opp = make_opportunity(deadline="2026-10-15T16:00:00-04:00")
    assert is_expired(opp, today=date(2026, 9, 24), forecast_max_age_days=180) is False


def test_forecast_without_deadline_expires_after_max_age():
    opp = make_opportunity(deadline=None, notice_type="grant_forecasted", posted_date="2026-01-01")
    assert is_expired(opp, today=date(2026, 9, 24), forecast_max_age_days=180) is True


def test_forecast_without_deadline_not_expired_within_max_age():
    opp = make_opportunity(deadline=None, notice_type="grant_forecasted", posted_date="2026-09-01")
    assert is_expired(opp, today=date(2026, 9, 24), forecast_max_age_days=180) is False


# --- merge_into_store ----------------------------------------------------------


def test_merge_into_store_marks_new_items():
    updated, new_ids, changed_ids = merge_into_store({}, [make_opportunity(id="sam:1")], now=NOW)
    assert new_ids == {"sam:1"}
    assert changed_ids == set()
    assert updated["sam:1"].first_seen_at == NOW
    assert updated["sam:1"].last_seen_at == NOW


def test_merge_into_store_preserves_first_seen_at_for_existing_items():
    existing = make_opportunity(id="sam:1", content_hash="hash1")
    existing.first_seen_at = NOW
    store = {"sam:1": existing}

    unchanged = make_opportunity(id="sam:1", content_hash="hash1")
    updated, new_ids, changed_ids = merge_into_store(store, [unchanged], now=LATER)

    assert new_ids == set()
    assert changed_ids == set()
    assert updated["sam:1"].first_seen_at == NOW
    assert updated["sam:1"].last_seen_at == LATER


def test_merge_into_store_detects_content_hash_change():
    existing = make_opportunity(id="sam:1", content_hash="hash1")
    existing.first_seen_at = NOW
    store = {"sam:1": existing}

    amended = make_opportunity(id="sam:1", content_hash="hash2")
    updated, new_ids, changed_ids = merge_into_store(store, [amended], now=LATER)

    assert new_ids == set()
    assert changed_ids == {"sam:1"}
    assert updated["sam:1"].content_hash == "hash2"
    assert updated["sam:1"].first_seen_at == NOW  # unchanged despite the amendment


# --- prune_store -----------------------------------------------------------


def test_prune_store_drops_expired_items():
    store = {
        "sam:1": make_opportunity(id="sam:1", deadline="2026-09-01T16:00:00-04:00"),
        "sam:2": make_opportunity(id="sam:2", deadline="2026-10-15T16:00:00-04:00"),
    }
    pruned = prune_store(
        store, today=date(2026, 9, 24), forecast_max_age_days=180, store_max_items=5000
    )
    assert set(pruned) == {"sam:2"}


def test_prune_store_drops_lowest_relevance_beyond_max_items():
    store = {
        "sam:1": make_opportunity(id="sam:1"),
        "sam:2": make_opportunity(id="sam:2"),
        "sam:3": make_opportunity(id="sam:3"),
    }
    pruned = prune_store(
        store,
        today=date(2026, 9, 24),
        forecast_max_age_days=180,
        store_max_items=2,
        relevance_by_id={"sam:1": 90, "sam:2": 10, "sam:3": 50},
    )
    assert set(pruned) == {"sam:1", "sam:3"}
