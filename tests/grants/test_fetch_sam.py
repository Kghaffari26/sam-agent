from datetime import date

import httpx
import pytest
from agents_core.http import HostPolicy, Http, RequestBudgetExceeded

from agents.grants.fetch_sam import (
    SAM_HOST,
    SamBudget,
    fetch_description,
    fetch_window,
    mdy,
    sam_window,
)
from agents.grants.state import SamState
from tests.grants.fakes import FixtureServer, load_fixture

TODAY = date(2026, 9, 26)


def make_http(tmp_path, srv: FixtureServer, daily_budget: int = 8) -> Http:
    http = Http(cache_dir=tmp_path / "cache", transport=srv.transport(), sleep=lambda s: None)
    http.set_policy(SAM_HOST, HostPolicy(daily_budget=daily_budget))
    return http


def page(n: int, total: int, start: int = 0) -> dict:
    return {
        "totalRecords": total,
        "opportunitiesData": [{"noticeId": f"n{start + i}"} for i in range(n)],
    }


# ---- window ----------------------------------------------------------------------


def test_first_run_window_uses_lookback():
    assert sam_window(
        SamState(), today=TODAY, overlap_days=1, first_run_lookback_days=7
    ) == (date(2026, 9, 19), TODAY)


def test_window_starts_at_last_posted_to_with_overlap():
    state = SamState(last_posted_to=date(2026, 9, 25))
    assert sam_window(state, today=TODAY, overlap_days=1, first_run_lookback_days=7) == (
        date(2026, 9, 24),
        TODAY,
    )


def test_lookback_override_and_one_year_max():
    state = SamState(last_posted_to=date(2020, 1, 1))
    start, end = sam_window(state, today=TODAY, overlap_days=1, first_run_lookback_days=7)
    assert (end - start).days == 364
    assert sam_window(
        state, today=TODAY, overlap_days=1, first_run_lookback_days=7, lookback_days=2
    ) == (date(2026, 9, 24), TODAY)


def test_mdy_format():
    assert mdy(date(2026, 9, 3)) == "09/03/2026"


# ---- search -----------------------------------------------------------------------


def test_request_params_lock_in_the_ptype_and_date_formats(tmp_path):
    srv = FixtureServer(sam_pages=[page(2, 2)])
    budget = SamBudget(make_http(tmp_path, srv), SamState(), daily_budget=8, today=TODAY)
    result = fetch_window(
        budget, "k", window=(date(2026, 9, 25), TODAY), ptypes=["o", "k", "p", "r"]
    )
    assert result.complete and len(result.records) == 2
    params = srv.calls[0].url.params
    assert params["ptype"] == "o,k,p,r"  # one comma-separated param, not repeated
    assert params.get_list("ptype") == ["o,k,p,r"]
    assert (params["postedFrom"], params["postedTo"]) == ("09/25/2026", "09/26/2026")
    assert params["limit"] == "1000" and params["offset"] == "0"
    assert params["api_key"] == "k"


def test_paginates_only_when_total_exceeds_limit(tmp_path):
    srv = FixtureServer(sam_pages=[page(1000, 1500), page(500, 1500, start=1000)])
    state = SamState()
    budget = SamBudget(make_http(tmp_path, srv), state, daily_budget=8, today=TODAY)
    result = fetch_window(budget, "k", window=(TODAY, TODAY), ptypes=["o"])
    assert result.complete and len(result.records) == 1500
    assert [c.url.params["offset"] for c in srv.calls] == ["0", "1000"]
    assert state.requests_today(TODAY) == 2


def test_budget_ledger_is_never_exceeded(tmp_path):
    srv = FixtureServer(sam_pages=[page(1000, 5000, start=i * 1000) for i in range(5)])
    state = SamState(requests={TODAY.isoformat(): 6})
    budget = SamBudget(make_http(tmp_path, srv), state, daily_budget=8, today=TODAY)
    result = fetch_window(budget, "k", window=(TODAY, TODAY), ptypes=["o"])
    assert len(srv.calls) == 2
    assert state.requests_today(TODAY) == 8
    assert result.budget_exhausted and not result.complete
    assert len(result.records) == 2000


def test_committed_ledger_binds_even_with_a_fresh_http_cache(tmp_path):
    # CI checkout: agents-core's own budget file is gone, the committed ledger isn't.
    srv = FixtureServer(sam_pages=[page(1, 1)])
    state = SamState(requests={TODAY.isoformat(): 8})
    budget = SamBudget(make_http(tmp_path, srv), state, daily_budget=8, today=TODAY)
    result = fetch_window(budget, "k", window=(TODAY, TODAY), ptypes=["o"])
    assert srv.calls == [] and result.budget_exhausted


def test_http_retries_cannot_overrun_the_ledger(tmp_path):
    calls = []

    def always_503(request):
        calls.append(request)
        return httpx.Response(503)

    http = Http(cache_dir=tmp_path / "c", transport=httpx.MockTransport(always_503),
                sleep=lambda s: None)
    http.set_policy(SAM_HOST, HostPolicy(daily_budget=8))
    state = SamState(requests={TODAY.isoformat(): 6})
    budget = SamBudget(http, state, daily_budget=8, today=TODAY)
    fetch_window(budget, "k", window=(TODAY, TODAY), ptypes=["o"])
    assert len(calls) == 2  # Http would retry 4x; the synced policy stops it at 2
    assert state.requests_today(TODAY) == 8


def test_cache_hits_are_free(tmp_path):
    srv = FixtureServer(sam_pages=[page(3, 3)])
    http = make_http(tmp_path, srv)
    state = SamState()
    for _ in range(2):
        budget = SamBudget(http, state, daily_budget=8, today=TODAY)
        fetch_window(budget, "k", window=(TODAY, TODAY), ptypes=["o"])
    assert len(srv.calls) == 1 and state.requests_today(TODAY) == 1


def test_404_is_zero_results_and_incomplete(tmp_path):
    srv = FixtureServer(sam_status=404)
    state = SamState()
    budget = SamBudget(make_http(tmp_path, srv), state, daily_budget=8, today=TODAY)
    result = fetch_window(budget, "k", window=(TODAY, TODAY), ptypes=["o"])
    assert result.empty_404 and not result.complete and result.records == []
    assert state.requests_today(TODAY) == 1  # it still cost a request


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_is_reported_not_raised(tmp_path, status):
    srv = FixtureServer(sam_status=status)
    budget = SamBudget(make_http(tmp_path, srv), SamState(), daily_budget=8, today=TODAY)
    result = fetch_window(budget, "k", window=(TODAY, TODAY), ptypes=["o"])
    assert result.auth_failed and result.error and not result.complete


def test_429_stops_sam_calls(tmp_path):
    srv = FixtureServer(sam_status=429)
    budget = SamBudget(make_http(tmp_path, srv, daily_budget=2), SamState(), daily_budget=2,
                       today=TODAY)
    result = fetch_window(budget, "k", window=(TODAY, TODAY), ptypes=["o"])
    assert result.budget_exhausted and len(srv.calls) == 2


def test_recorded_live_fixture_parses(tmp_path):
    data = load_fixture("sam_search_live.json")
    data["totalRecords"] = len(data["opportunitiesData"])  # trimmed from the live page
    srv = FixtureServer(sam_pages=[data])
    budget = SamBudget(make_http(tmp_path, srv), SamState(), daily_budget=8, today=TODAY)
    result = fetch_window(budget, "k", window=(TODAY, TODAY), ptypes=["o", "k", "p", "r"])
    assert len(result.records) == len(data["opportunitiesData"]) > 0


# ---- descriptions -----------------------------------------------------------------


def test_description_fetch_costs_one_request_and_404_is_empty(tmp_path):
    srv = FixtureServer(sam_descriptions={"abc": "<p>Build a dashboard.</p>"})
    state = SamState()
    budget = SamBudget(make_http(tmp_path, srv), state, daily_budget=8, today=TODAY)
    url = "https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid="
    assert fetch_description(budget, "k", url + "abc") == "<p>Build a dashboard.</p>"
    assert fetch_description(budget, "k", url + "missing") == ""
    assert state.requests_today(TODAY) == 2
    assert srv.calls[0].url.params["noticeid"] == "abc"
    assert srv.calls[0].url.params["api_key"] == "k"


def test_description_fetch_respects_budget(tmp_path):
    srv = FixtureServer(sam_descriptions={"abc": "x"})
    state = SamState(requests={TODAY.isoformat(): 8})
    budget = SamBudget(make_http(tmp_path, srv), state, daily_budget=8, today=TODAY)
    with pytest.raises(RequestBudgetExceeded):
        fetch_description(budget, "k", "https://api.sam.gov/x?noticeid=abc")
    assert srv.calls == []


def test_non_sam_urls_are_refused(tmp_path):
    budget = SamBudget(make_http(tmp_path, FixtureServer()), SamState(), daily_budget=8,
                       today=TODAY)
    with pytest.raises(ValueError):
        budget.get("https://example.com/x", {})


def test_cached_search_is_served_even_when_the_budget_is_used_up(tmp_path):
    srv = FixtureServer(sam_pages=[page(3, 3)])
    http = make_http(tmp_path, srv)
    state = SamState()
    fetch_window(SamBudget(http, state, daily_budget=1, today=TODAY), "k",
                 window=(TODAY, TODAY), ptypes=["o"])
    assert state.requests_today(TODAY) == 1
    again = fetch_window(SamBudget(http, state, daily_budget=1, today=TODAY), "k",
                         window=(TODAY, TODAY), ptypes=["o"])
    assert again.complete and len(again.records) == 3
    assert len(srv.calls) == 1 and state.requests_today(TODAY) == 1
