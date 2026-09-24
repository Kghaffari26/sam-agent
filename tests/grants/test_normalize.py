import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agents.grants.normalize import (
    compute_content_hash,
    normalize_grants_gov,
    normalize_sam,
    parse_deadline,
    parse_mdy_date,
    strip_html,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "grants"
NOW = datetime(2026, 9, 24, tzinfo=UTC)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def sam_notices() -> list[dict]:
    return load_fixture("sam_opportunities_search.json")["opportunitiesData"]


@pytest.fixture
def sam_descriptions() -> dict[str, str]:
    return load_fixture("sam_descriptions.json")


# --- strip_html / parse_deadline / parse_mdy_date -----------------------------


def test_strip_html_removes_tags_and_collapses_whitespace():
    assert strip_html("<p>Hello   <b>world</b>, there.</p>") == "Hello world, there."


def test_strip_html_none_stays_none():
    assert strip_html(None) is None
    assert strip_html("") is None


def test_parse_deadline_with_offset_is_not_assumed():
    deadline, tz_assumed = parse_deadline("2026-10-15T16:00:00-04:00")
    assert tz_assumed is False
    assert deadline.utcoffset().total_seconds() == -4 * 3600


def test_parse_deadline_without_offset_assumes_eastern():
    deadline, tz_assumed = parse_deadline("2026-10-06T17:00:00")
    assert tz_assumed is True
    assert deadline.tzinfo is not None
    assert deadline.year == 2026 and deadline.month == 10 and deadline.day == 6


def test_parse_deadline_none_or_empty():
    assert parse_deadline(None) == (None, False)
    assert parse_deadline("") == (None, False)


def test_parse_mdy_date():
    assert parse_mdy_date("11/01/2026").isoformat() == "2026-11-01"
    assert parse_mdy_date(None) is None


# --- content hash --------------------------------------------------------------


def test_content_hash_stable_and_order_independent():
    a = compute_content_hash({"title": "X", "deadline": None})
    b = compute_content_hash({"deadline": None, "title": "X"})
    assert a == b


def test_content_hash_changes_with_content():
    a = compute_content_hash({"title": "X"})
    b = compute_content_hash({"title": "Y"})
    assert a != b


# --- normalize_sam ---------------------------------------------------------


def test_normalize_sam_solicitation_with_offset_deadline(sam_notices, sam_descriptions):
    raw = sam_notices[0]
    opp = normalize_sam(raw, now=NOW, description_text=sam_descriptions[raw["noticeId"]])

    assert opp.id == "sam:5b345bbb7127b91a3ad577b203fc6f68"
    assert opp.source == "sam"
    assert opp.kind == "contract"
    assert opp.notice_type == "solicitation"
    assert opp.agency == "VETERANS AFFAIRS, DEPARTMENT OF"
    assert opp.sub_agency == "DEPARTMENT OF VETERANS AFFAIRS"
    assert opp.office == "NETWORK CONTRACTING OFFICE 10"
    assert opp.naics == ["541512"]
    assert opp.set_aside_code == "SBA"
    assert opp.set_aside_label == "Total Small Business Set-Aside"
    assert opp.deadline_tz_assumed is False
    assert opp.place_state == "DC"
    assert opp.place_city == "Washington"
    assert opp.description_fetched is True
    assert "Python" in opp.description_text
    assert "<b>" not in opp.description_text
    assert str(opp.url) == "https://sam.gov/opp/5b345bbb7127b91a3ad577b203fc6f68/view"


def test_normalize_sam_deadline_without_timezone_is_assumed(sam_notices):
    raw = sam_notices[1]  # Sources Sought, naive responseDeadLine
    opp = normalize_sam(raw, now=NOW)

    assert opp.notice_type == "sources_sought"
    assert opp.deadline_tz_assumed is True
    assert opp.deadline.tzinfo is not None
    assert opp.set_aside_code is None
    assert opp.description_fetched is False
    assert opp.description_text is None


def test_normalize_sam_8a_set_aside_label_falls_back_to_setasides_module(sam_notices):
    raw = sam_notices[2]
    opp = normalize_sam(raw, now=NOW)
    assert opp.set_aside_code == "8AN"
    assert opp.set_aside_label == "8(a) Set-Aside"
    assert opp.place_state is None
    assert opp.place_city is None


def test_normalize_sam_content_hash_changes_when_description_arrives(sam_notices, sam_descriptions):
    raw = sam_notices[0]
    without_desc = normalize_sam(raw, now=NOW)
    with_desc = normalize_sam(raw, now=NOW, description_text=sam_descriptions[raw["noticeId"]])
    assert without_desc.content_hash != with_desc.content_hash


def test_normalize_sam_unknown_type_raises():
    raw = {
        "noticeId": "x",
        "title": "x",
        "type": "Award Notice",
        "postedDate": "2026-01-01",
        "uiLink": "https://sam.gov/opp/x/view",
    }
    with pytest.raises(ValueError):
        normalize_sam(raw, now=NOW)


# --- normalize_grants_gov ---------------------------------------------------


@pytest.fixture
def grants_gov_hits() -> list[dict]:
    return load_fixture("grants_gov_search2.json")["data"]["oppHits"]


def test_normalize_grants_gov_without_detail(grants_gov_hits):
    hit = grants_gov_hits[0]
    opp = normalize_grants_gov(hit, now=NOW)

    assert opp.id == "gg:355678"
    assert opp.source == "grants_gov"
    assert opp.kind == "grant"
    assert opp.notice_type == "grant_posted"
    assert opp.aln == ["10.310"]
    assert opp.eligibility_codes == []
    assert opp.description_fetched is False
    # Grants.gov close dates carry no time zone; end-of-day US/Eastern is assumed.
    assert opp.deadline_tz_assumed is True
    assert opp.deadline.hour == 23 and opp.deadline.minute == 59


def test_normalize_grants_gov_forecast_has_no_deadline(grants_gov_hits):
    hit = grants_gov_hits[2]
    opp = normalize_grants_gov(hit, now=NOW)
    assert opp.notice_type == "grant_forecasted"
    assert opp.deadline is None
    assert opp.deadline_tz_assumed is False


def test_normalize_grants_gov_with_detail_fills_eligibility_and_value(grants_gov_hits):
    hit = grants_gov_hits[0]
    detail = load_fixture("grants_gov_fetch_opportunity_355678.json")["data"]
    opp = normalize_grants_gov(hit, now=NOW, detail=detail)

    assert opp.description_fetched is True
    assert "agricultural" in opp.description_text.lower()
    assert set(opp.eligibility_codes) == {"23", "06", "25"}
    assert opp.value_kind == "award_ceiling"
    assert opp.value_amount == 750000.0
    assert opp.value_floor == 100000.0


def test_normalize_grants_gov_content_hash_changes_with_detail(grants_gov_hits):
    hit = grants_gov_hits[0]
    detail = load_fixture("grants_gov_fetch_opportunity_355678.json")["data"]
    without_detail = normalize_grants_gov(hit, now=NOW)
    with_detail = normalize_grants_gov(hit, now=NOW, detail=detail)
    assert without_detail.content_hash != with_detail.content_hash
