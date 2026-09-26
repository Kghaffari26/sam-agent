from datetime import UTC, datetime

import pytest
from agents_core.http import Http

from agents.grants.fetch_grants_gov import (
    DetailCache,
    GrantsGovSchemaError,
    detail_key,
    fetch_details,
    search_all,
    trim_detail,
)
from agents.grants.normalize import normalize_grants_gov
from tests.grants.fakes import FixtureServer, load_fixture

NOW = datetime(2026, 9, 26, tzinfo=UTC)
LIVE_SEARCH = "grants_gov_search2_live.json"
LIVE_POSTED = "grants_gov_fetch_opportunity_live_posted.json"
LIVE_FORECAST = "grants_gov_fetch_opportunity_live_forecasted.json"


def make_http(tmp_path, srv):
    return Http(cache_dir=tmp_path / "cache", transport=srv.transport(), sleep=lambda s: None)


def test_one_search_per_keyword_unioned_and_deduped(tmp_path):
    srv = FixtureServer(gg_search=load_fixture(LIVE_SEARCH))
    hits = search_all(make_http(tmp_path, srv), ["software", "data", "AI"],
                      opp_statuses="forecasted|posted", rows=100)
    bodies = [c.content for c in srv.calls_to("api.grants.gov", "search2")]
    assert len(bodies) == 3
    ids = [h["id"] for h in hits]
    assert len(ids) == len(set(ids)) == len(load_fixture(LIVE_SEARCH)["data"]["oppHits"])
    assert b'"rows":100' in bodies[0] and b'"oppStatuses":"forecasted|posted"' in bodies[0]
    assert srv.calls[0].headers["user-agent"].startswith("sam-agent/")


def test_missing_opp_hits_is_a_schema_error(tmp_path):
    srv = FixtureServer(gg_search={"errorcode": 0, "data": {"hitCount": 1}})
    with pytest.raises(GrantsGovSchemaError, match="oppHits"):
        search_all(make_http(tmp_path, srv), ["x"], opp_statuses="posted", rows=5)


def test_detail_cache_fetches_once_per_id_and_close_date(tmp_path):
    hit = {"id": "355439", "closeDate": "12/04/2026"}
    srv = FixtureServer(gg_details={"355439": load_fixture(LIVE_POSTED)})
    http = make_http(tmp_path, srv)
    cache = DetailCache()
    assert fetch_details(http, [hit], cache, max_fetches=60) == 1
    assert fetch_details(http, [hit], cache, max_fetches=60) == 0
    path = tmp_path / "details.json.gz"
    cache.save(path)
    reloaded = DetailCache.load(path)
    assert reloaded.get(hit) is not None
    # An extended close date is a new key: fetched again.
    extended = {**hit, "closeDate": "01/15/2027"}
    assert reloaded.get(extended) is None
    assert detail_key(extended) == "355439|01/15/2027"


def test_detail_fetch_cap_and_failures(tmp_path):
    hits = [{"id": str(i), "closeDate": ""} for i in range(10)]
    srv = FixtureServer(gg_details={"1": load_fixture(LIVE_POSTED)})
    cache = DetailCache()
    assert fetch_details(make_http(tmp_path, srv), hits, cache, max_fetches=3) == 1
    assert len(srv.calls) == 6  # "0" fails, "1" ok, then 4 more failures hit the stop limit


def test_prune_keeps_only_ids_still_searched():
    cache = DetailCache({"1|": {}, "2|": {}})
    cache.prune([{"id": "2", "closeDate": ""}])
    assert set(cache.entries) == {"2|"}


def test_live_posted_detail_normalizes():
    hit = {"id": "355439", "number": "RFA-OD-24-011", "title": "x", "agency": "NIH",
           "openDate": "07/17/2024", "closeDate": "12/04/2026", "oppStatus": "posted",
           "cfdaList": ["93.310"]}
    detail = trim_detail(load_fixture(LIVE_POSTED)["data"])
    opp = normalize_grants_gov(hit, now=NOW, detail=detail)
    assert opp.title.startswith("NIH Research Software Engineer")
    assert opp.description_fetched and opp.description_text
    assert opp.eligibility_codes  # nested under synopsis in the live API
    assert opp.value_amount is None  # awardCeiling is the string "none"
    assert len(opp.aln) > 1


def test_live_forecast_detail_normalizes():
    raw = load_fixture(LIVE_FORECAST)["data"]
    hit = {"id": str(raw["id"]), "title": raw["opportunityTitle"], "openDate": "02/05/2026",
           "closeDate": "", "oppStatus": "forecasted", "cfdaList": []}
    opp = normalize_grants_gov(hit, now=NOW, detail=trim_detail(raw))
    assert opp.notice_type == "grant_forecasted" and opp.deadline is None
    assert opp.description_text and "<" not in opp.description_text  # forecastDesc, stripped
    assert "23" in opp.eligibility_codes


def test_trim_detail_is_small():
    import json

    raw = load_fixture(LIVE_POSTED)["data"]
    assert len(json.dumps(trim_detail(raw))) < len(json.dumps(raw)) / 2
