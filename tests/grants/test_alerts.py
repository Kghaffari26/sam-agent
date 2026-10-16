"""Email alerts (scope S4): selection, dedupe, the daily cap, rendering, delivery,
the no-secret path and the dry run."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
from agents_core.http import Http

from agents.grants import alerts
from agents.grants.config import AlertSettings, load_grants_config
from agents.grants.models import Score, SubScores
from agents.grants.store import StoreEntry, save_store
from tests.grants.conftest import make_opportunity

NOW = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
TODAY = NOW.date()
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "grants"
SETTINGS = AlertSettings(enabled=True, min_fit=80, recommendations=["Pursue"], max_per_day=5,
                         site_url="https://hub.example.com/grants")
SECRETS = {alerts.KEY_ENV: "re_test", alerts.TO_ENV: "owner@example.com"}


def entry(id_, fit, rec="Pursue", content_hash="h1", **opp):
    opp.setdefault("deadline", "2026-11-02T16:00:00-05:00")
    o = make_opportunity(id=id_, source_id=id_.split(":")[1], content_hash=content_hash,
                         url=f"https://sam.gov/opp/{id_.split(':')[1]}/view",
                         title=f"Opportunity {id_}", agency="GENERAL SERVICES ADMINISTRATION",
                         sub_agency="FEDERAL ACQUISITION SERVICE", **opp)
    s = Score(opportunity_id=id_, sub_scores=SubScores(capability=30, eligibility=20, size=10,
                                                       timeline=10, strategic=10),
              fit=fit, recommendation=rec, reasons=[f"Reason one for {id_}", "Reason two",
                                                     "Reason three"],
              red_flags=[], confidence="high", model_id="m", profile_hash="p",
              prompt_version="v2", content_hash=content_hash, scored_at=NOW)
    return StoreEntry(opportunity=o, relevance=50, score=s)


def store(*entries):
    return {e.opportunity.id: e for e in entries}


def ids(selected):
    return [e.id for e in selected]


def test_threshold_and_recommendation():
    s = store(entry("sam:a", 85), entry("sam:b", 80), entry("sam:c", 79),
              entry("sam:d", 90, rec="Consider"),
              entry("sam:e", 88, deadline="2026-10-15T16:00:00-04:00"))  # closed
    assert ids(alerts.select(s, alerts.AlertState(), SETTINGS, today=TODAY)) == [
        "sam:a", "sam:b"]


def test_no_repeats_but_an_amended_notice_alerts_again():
    state = alerts.AlertState()
    s = store(entry("sam:a", 85), entry("sam:b", 82))
    first = alerts.select(s, state, SETTINGS, today=TODAY)
    alerts.mark_sent(state, first, now=NOW)
    assert alerts.select(s, state, SETTINGS, today=TODAY) == []
    amended = store(entry("sam:a", 85, content_hash="h2"), entry("sam:b", 82))
    [again] = alerts.select(amended, state, SETTINGS, today=TODAY)
    assert again.id == "sam:a" and again.updated


def test_a_rescored_item_crossing_the_threshold_is_alerted_once():
    state = alerts.AlertState()
    assert alerts.select(store(entry("sam:a", 78)), state, SETTINGS, today=TODAY) == []
    [crossed] = alerts.select(store(entry("sam:a", 81)), state, SETTINGS, today=TODAY)
    alerts.mark_sent(state, [crossed], now=NOW)
    # It dips under and comes back at the same content hash: no repeat.
    assert alerts.select(store(entry("sam:a", 83)), state, SETTINGS, today=TODAY) == []


def test_max_per_day_best_fit_first_and_the_rest_tomorrow():
    state = alerts.AlertState()
    s = store(*(entry(f"sam:x{i}", 80 + i) for i in range(7)))
    first = alerts.select(s, state, SETTINGS, today=TODAY)
    assert ids(first) == [f"sam:x{i}" for i in (6, 5, 4, 3, 2)]
    alerts.mark_sent(state, first, now=NOW)
    assert alerts.select(s, state, SETTINGS, today=TODAY) == []
    assert ids(alerts.select(s, state, SETTINGS, today=date(2026, 10, 17))) == [
        "sam:x1", "sam:x0"]


def digest_entries():
    return alerts.select(store(
        entry("sam:a", 86, deadline="2026-10-30T14:00:00-04:00", value_kind="award_ceiling",
              value_amount=750000.0, set_aside_label="Total Small Business Set-Aside"),
        entry("sam:b", 81, deadline="2026-11-20T16:00:00-05:00"),
    ), alerts.AlertState(), SETTINGS, today=TODAY)


def test_digest_snapshot():
    entries = digest_entries()
    text = alerts.render_text(entries, today=TODAY)
    page = alerts.render_html(entries, today=TODAY)
    assert alerts.subject(entries) == "Grants Finder: 2 strong new matches (best fit 86)"
    assert text == (FIXTURES / "alert_digest.txt").read_text()
    assert page == (FIXTURES / "alert_digest.html").read_text()
    assert "<img" not in page and "enabled = false" in page


def write_store(tmp_path, *entries):
    path = tmp_path / "store.json.gz"
    save_store(store(*entries), path)
    return path


def resend(calls, status=200):
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"id": "email_123"} if status == 200 else
                              {"message": "invalid key"})
    return Http(cache_dir=Path("/nonexistent-cache"), transport=httpx.MockTransport(handler),
                sleep=lambda s: None)


def test_one_digest_is_sent_and_recorded(tmp_path):
    calls = []
    path = write_store(tmp_path, entry("sam:a", 85), entry("sam:b", 82))
    state_file = tmp_path / "alerts.json"
    kw = dict(dry_run=False, env=SECRETS, store_path=path, state_file=state_file,
              settings=SETTINGS)
    status = alerts.run(now=NOW, http=resend(calls), **kw)
    assert status == "ok: sent 2 (email_123)"
    [req] = calls
    body = json.loads(req.content)
    assert req.url == alerts.RESEND_URL and req.headers["authorization"] == "Bearer re_test"
    assert body["to"] == ["owner@example.com"] and "sam:a" not in body["subject"]
    assert "https://hub.example.com/grants#sam:a" in body["text"]
    state = alerts.load_state(state_file)
    assert set(state.alerted) == {"sam:a", "sam:b"} and state.sent_per_day == {"2026-10-16": 2}
    # The second run sends nothing new (and makes no request).
    assert alerts.run(now=NOW, http=resend(calls), **kw) == "ok: nothing new"
    assert len(calls) == 1


def test_a_delivery_error_is_recorded_and_retried(tmp_path):
    calls = []
    path = write_store(tmp_path, entry("sam:a", 85))
    state_file = tmp_path / "alerts.json"
    status = alerts.run(dry_run=False, env=SECRETS, now=NOW, store_path=path,
                        state_file=state_file, settings=SETTINGS, http=resend(calls, 403))
    assert status.startswith("failed") and len(calls) == 1  # one attempt, no retry
    assert alerts.load_state(state_file).alerted == {}
    assert "failed" in alerts.warning_for(state_file, SETTINGS)


def test_missing_secrets_skip_with_a_warning(tmp_path):
    path = write_store(tmp_path, entry("sam:a", 85))
    state_file = tmp_path / "alerts.json"
    status = alerts.run(dry_run=False, env={alerts.KEY_ENV: "re_test"}, now=NOW,
                        store_path=path, state_file=state_file, settings=SETTINGS)
    assert status == alerts.SKIPPED_NO_SECRETS
    assert alerts.load_state(state_file).alerted == {}
    warning = alerts.warning_for(state_file, SETTINGS)
    assert alerts.TO_ENV in warning and "enabled = false" in warning
    assert alerts.warning_for(state_file, SETTINGS.model_copy(update={"enabled": False})) is None


def test_dry_run_writes_the_job_summary_and_changes_nothing(tmp_path):
    path = write_store(tmp_path, entry("sam:a", 85))
    state_file, summary = tmp_path / "alerts.json", tmp_path / "summary.md"
    status = alerts.run(dry_run=True, env=SECRETS, now=NOW, store_path=path,
                        state_file=state_file, settings=SETTINGS, summary_path=summary)
    assert status == "dry run: 1 would be sent"
    assert "not sent" in summary.read_text() and "Opportunity sam:a" in summary.read_text()
    assert not state_file.exists()


def test_disabled_does_nothing(tmp_path):
    path = write_store(tmp_path, entry("sam:a", 85))
    off = SETTINGS.model_copy(update={"enabled": False})
    assert alerts.run(dry_run=False, env=SECRETS, now=NOW, store_path=path,
                      state_file=tmp_path / "a.json", settings=off) == "disabled"


def test_committed_config_has_the_scoped_defaults():
    a = load_grants_config("config/grants.toml").alerts
    assert (a.enabled, a.min_fit, a.recommendations, a.max_per_day) == (
        True, 80, ["Pursue"], 5)
    assert not hasattr(a, "to")  # the recipient is a secret, never config
