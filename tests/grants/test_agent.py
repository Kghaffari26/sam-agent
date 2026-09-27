"""End to end through agents-core's real runner: fetch -> transform -> analyze
-> validate -> publish, against fixtures and a fake Anthropic client."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agents_core import registry, runner
from agents_core.http import Http

from agents.grants.agent import AGENT, GrantsAgent, parse_options
from agents.grants.schema import GrantsAll, GrantsLatest
from tests.grants.fakes import FakeClient, FixtureServer, first_text, load_fixture

REPO_ROOT = Path(__file__).resolve().parents[2]
FROZEN = datetime(2026, 9, 24, 13, 0, 5, tzinfo=UTC)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D102 - test clock
        return FROZEN if tz is None else FROZEN.astimezone(tz)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("AGENTS_CORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENTS_CORE_PUBLISH_DIR", str(tmp_path / "public-data"))
    monkeypatch.setenv("AGENTS_CORE_MAX_RUN_USD", "0.50")
    monkeypatch.setenv("SAM_API_KEY", "test-key-not-real")
    monkeypatch.setattr(runner, "datetime", _FrozenDatetime)
    return tmp_path


def server() -> FixtureServer:
    return FixtureServer(
        sam_pages=[load_fixture("sam_opportunities_search.json")],
        sam_descriptions=load_fixture("sam_descriptions.json"),
        gg_search=load_fixture("grants_gov_search2.json"),
        gg_details={
            "355678": load_fixture("grants_gov_fetch_opportunity_355678.json"),
            "358120": load_fixture("grants_gov_fetch_opportunity_358120.json"),
        },
    )


def run_once(tmp_path: Path, srv: FixtureServer, client: FakeClient, *args: str,
             dry_run: bool = False) -> int:
    http = Http(cache_dir=tmp_path / "http-cache", transport=srv.transport(), sleep=lambda s: None)
    try:
        return runner.run(AGENT, dry_run=dry_run, extra_args=list(args), http=http,
                          llm_client=client)
    finally:
        http.close()


def read(tmp_path: Path, name: str) -> dict:
    return json.loads((tmp_path / "public-data" / name).read_text())


def test_registered_as_grants_entry_point():
    assert registry.discover_agents()["grants"] == "agents.grants.agent:AGENT"
    assert isinstance(registry.load("grants"), GrantsAgent)


def test_parse_options():
    opts = parse_options(["--rescore-all", "--lookback-days=2", "--sam-request-budget", "3"])
    assert (opts.rescore_all, opts.lookback_days, opts.sam_request_budget) == (True, 2, 3)


def test_dry_run_makes_no_llm_calls_and_publishes_nothing(env):
    srv, client = server(), FakeClient()
    assert run_once(env, srv, client, dry_run=True) == 0
    assert client.total_calls == 0
    assert not (env / "public-data" / "latest.json").exists()
    # The SAM ledger is still recorded (a dry run fetches within budget).
    state = json.loads((env / "data" / "grants" / "state.json").read_text())
    assert state["sam"]["requests"] == {"2026-09-24": 1}
    assert state["sam"]["last_posted_to"] is None  # window only advances on a full run


def test_full_run_publishes_the_data_branch_contract(env):
    srv, client = server(), FakeClient()
    assert run_once(env, srv, client) == 0
    pub = env / "public-data"
    for name in ("latest.json", "all.json", "manifest-entry.json", "costs-summary.json",
                 "schema.json", "history/2026-09-24.json"):
        assert (pub / name).is_file(), name

    latest = GrantsLatest.model_validate(read(env, "latest.json"))
    GrantsAll.model_validate(read(env, "all.json"))
    assert latest.meta.agent == "grants"
    assert latest.meta.sam_requests_used >= 1
    assert latest.top_matches, "expected scored top matches"
    for m in latest.top_matches:
        assert m.fit == sum(m.sub_scores.model_dump().values()) or m.fit in (20, 70)
        assert m.summary is not None and m.summary.next_steps
    assert set(read(env, "latest.json")["meta"]) >= {"sam_budget_exhausted", "sam_requests_used"}
    assert "sam_meta" not in read(env, "latest.json")
    manifest = read(env, "manifest-entry.json")
    assert manifest["id"] == "grants" and manifest["route"] == "/grants"
    assert manifest["items_count"] == latest.stats.active_matches
    assert (pub / "latest.json").stat().st_size <= 200_000
    assert (pub / "all.json").stat().st_size <= 1_000_000
    # Batch API for scoring, sync for summaries.
    assert client.batch_requests and client.sync_calls


def test_second_run_makes_zero_llm_calls_and_zero_description_fetches(env):
    srv = server()
    first = FakeClient()
    assert run_once(env, srv, first) == 0
    desc_before = len([c for c in srv.calls_to("api.sam.gov") if "search" not in c.url.path])

    second = FakeClient()
    assert run_once(env, srv, second) == 0
    desc_after = len([c for c in srv.calls_to("api.sam.gov") if "search" not in c.url.path])
    # Scores and summaries are cached; bid research picks up the 4th Pursue match
    # (3 per run), and the three researched on the first run are served from cache.
    assert not second.sync_calls and not second.batch_requests
    researched = {first_text(c) for c in second.research_calls}
    assert len(researched) == 1
    assert desc_after == desc_before
    latest = read(env, "latest.json")
    assert latest["stats"]["llm_scored_this_run"] == 0
    assert latest["stats"]["llm_scored_cached"] >= len(latest["top_matches"])

    third = FakeClient()
    assert run_once(env, srv, third) == 0
    assert third.total_calls == 0
    pursue = [m for m in read(env, "latest.json")["top_matches"]
              if m["recommendation"] == "Pursue"]
    assert len(pursue) == 4 and all(m["research"] for m in pursue)


def test_profile_change_triggers_rescore(env, monkeypatch):
    srv = server()
    assert run_once(env, srv, FakeClient()) == 0
    import agents.grants.agent as agent_mod

    real = agent_mod.load_business_profile

    def edited(path):
        profile = real(path)
        return profile.model_copy(update={"team_capacity_note": "Can staff 6 people."})

    monkeypatch.setattr(agent_mod, "load_business_profile", edited)
    again = FakeClient()
    assert run_once(env, srv, again) == 0
    assert again.batch_requests, "profile edit should invalidate cached scores"


def test_sam_budget_is_never_exceeded(env):
    srv = server()
    assert run_once(env, srv, FakeClient(), "--sam-request-budget=1") == 0
    assert len(srv.calls_to("api.sam.gov")) == 1  # the search; no description fetches
    latest = read(env, "latest.json")
    assert latest["meta"]["sam_requests_used"] == 1
    assert latest["meta"]["sam_budget_exhausted"] is True
    assert run_once(env, srv, FakeClient(), "--sam-request-budget=1") == 0
    assert len(srv.calls_to("api.sam.gov")) == 1  # served from cache, budget untouched


def test_sam_404_is_empty_and_does_not_advance_window(env):
    srv = server()
    srv.sam_status = 404
    assert run_once(env, srv, FakeClient()) == 0
    state = json.loads((env / "data" / "grants" / "state.json").read_text())
    assert state["sam"]["last_posted_to"] is None
    assert read(env, "latest.json")["stats"]["fetched"]["sam"] == 0


def test_grants_gov_schema_change_fails_only_that_portion(env):
    srv = server()
    srv.gg_search = {"errorcode": 0, "data": {"hitCount": 3}}  # no oppHits
    assert run_once(env, srv, FakeClient()) == 0
    latest = read(env, "latest.json")
    assert latest["stats"]["fetched"]["grants_gov"] == 0
    assert latest["stats"]["fetched"]["sam"] > 0


def test_no_sam_key_never_calls_sam(env, monkeypatch):
    monkeypatch.delenv("SAM_API_KEY")
    srv = server()
    assert run_once(env, srv, FakeClient()) == 0
    assert srv.calls_to("api.sam.gov") == []


# ---- agents-core v0.2.0/v0.3.0: warnings, alerts, no-key fallback, tracing ----------


@pytest.fixture
def alerts(monkeypatch):
    """Record ctx.alert calls instead of opening GitHub issues."""
    import agents_core.alerts

    sent: list[tuple[str, str]] = []

    def fake(title, body, **kwargs):
        sent.append((title, body))
        return "created"

    monkeypatch.setattr(agents_core.alerts, "ops_alert", fake)
    return sent


def test_no_anthropic_key_publishes_ok_with_a_warning(env):
    """agents-hub report: a missing key used to crash the run (RuntimeError from
    the SDK client). Now: unscored output, status ok, a warning."""
    srv = server()
    assert run_once(env, srv, None) == 0
    latest = read(env, "latest.json")
    assert latest["meta"]["status"] == "ok"
    assert any("No Anthropic API key" in w for w in latest["meta"]["warnings"])
    assert latest["top_matches"] == [] and latest["stats"]["llm_scored_this_run"] == 0
    assert all(r["fit"] is None and not r["has_research"] for r in read(env, "all.json")["rows"])
    assert read(env, "manifest-entry.json")["status"] == "ok"


def test_no_anthropic_key_after_a_keyed_run_keeps_cached_work(env):
    srv = server()
    assert run_once(env, srv, FakeClient()) == 0
    before = read(env, "latest.json")
    assert run_once(env, srv, None) == 0
    latest = read(env, "latest.json")
    assert latest["meta"]["status"] == "ok" and latest["meta"]["warnings"]
    assert [m["id"] for m in latest["top_matches"]] == [m["id"] for m in before["top_matches"]]
    assert all(m["summary"]["next_steps"] for m in latest["top_matches"])
    researched = [m for m in latest["top_matches"] if m["research"]]
    assert len(researched) == 3  # cached research is still published


def test_trace_json_is_published_with_agent_spans(env):
    assert run_once(env, server(), FakeClient()) == 0
    trace = read(env, "trace.json")
    names = {(s["kind"], s["name"]) for s in trace["spans"]}
    for step in ("score candidates", "fetch top descriptions", "summaries", "bid research"):
        assert ("custom", step) in names, step
    assert any(k == "agent_loop" and n.startswith("research:") for k, n in names)
    assert any(k == "tool_call" and n == "get_opportunity" for k, n in names)
    assert trace["summary"]["tool_calls"] >= 6 and trace["summary"]["llm_calls"] > 0
    assert (env / "public-data" / "trace.schema.json").is_file()
    assert read(env, "manifest-entry.json")["trace_summary"]["steps"] >= 6
    assert "test-key-not-real" not in (env / "public-data" / "trace.json").read_text()


def test_research_is_published_additively(env):
    assert run_once(env, server(), FakeClient()) == 0
    latest = GrantsLatest.model_validate(read(env, "latest.json"))
    researched = [m for m in latest.top_matches if m.research is not None]
    assert len(researched) == 3 and all(m.recommendation == "Pursue" for m in researched)
    r = researched[0].research
    assert r.status == "complete" and r.narrative_source == "llm" and r.citations
    assert r.tools_used[0] == "get_opportunity" and r.steps == 2
    rows = {row["id"]: row for row in read(env, "all.json")["rows"]}
    assert {i for i, row in rows.items() if row["has_research"]} == {m.id for m in researched}


def test_sam_key_rejected_warns_and_alerts(env, alerts):
    srv = server()
    srv.sam_status = 403
    assert run_once(env, srv, FakeClient()) == 0
    latest = read(env, "latest.json")
    assert latest["meta"]["status"] == "ok"  # Grants.gov still published
    assert any("SAM API key rejected (403)" in w for w in latest["meta"]["warnings"])
    assert [t for t, _ in alerts] == ["SAM.gov API key rejected"]
    state = json.loads((env / "data" / "grants" / "state.json").read_text())
    assert state["sam"]["key_rejected_at"] == "2026-09-24"


def test_sam_budget_exhaustion_warns_and_alerts(env, alerts):
    srv = server()
    srv.sam_pages = [dict(load_fixture("sam_opportunities_search.json"), totalRecords=5000)]
    assert run_once(env, srv, FakeClient(), "--sam-request-budget=1") == 0
    latest = read(env, "latest.json")
    assert latest["meta"]["sam_budget_exhausted"] is True
    assert any("daily request budget ran out" in w for w in latest["meta"]["warnings"])
    assert "SAM.gov request budget exhausted" in [t for t, _ in alerts]


@pytest.mark.parametrize("expires,title,warned", [
    ("2026-10-01", "SAM.gov API key expires soon", False),
    ("2026-09-01", "SAM.gov API key expired", True),
])
def test_sam_key_expiry_alerts(env, alerts, monkeypatch, expires, title, warned):
    import agents.grants.agent as agent_mod

    real = agent_mod.load_grants_config

    def with_expiry(path):
        config = real(path)
        return config.model_copy(update={"sam_key": config.sam_key.model_copy(
            update={"expires_on": datetime.fromisoformat(expires).date()})})

    monkeypatch.setattr(agent_mod, "load_grants_config", with_expiry)
    assert run_once(env, server(), FakeClient()) == 0
    assert title in [t for t, _ in alerts]
    warnings = read(env, "latest.json")["meta"]["warnings"]
    assert any("expired" in w for w in warnings) is warned


def test_max_run_usd_warns_and_alerts(env, alerts, monkeypatch):
    monkeypatch.setenv("AGENTS_CORE_MAX_RUN_USD", "0.003")
    assert run_once(env, server(), FakeClient()) == 0
    latest = read(env, "latest.json")
    assert latest["meta"]["status"] == "ok"
    assert any(w.startswith("MAX_RUN_USD reached") for w in latest["meta"]["warnings"])
    assert "Grants agent hit MAX_RUN_USD" in [t for t, _ in alerts]


def test_warnings_are_empty_on_a_clean_run(env, alerts):
    assert run_once(env, server(), FakeClient()) == 0
    assert read(env, "latest.json")["meta"]["warnings"] == [] and alerts == []
