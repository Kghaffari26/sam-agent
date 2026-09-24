from datetime import date

from agents.grants.state import (
    GrantsState,
    SamState,
    load_state,
    profile_changed,
    save_state,
)


def test_default_state_is_empty():
    state = GrantsState()
    assert state.sam.last_posted_to is None
    assert state.sam.requests == {}
    assert state.profile_hash is None
    assert state.prompt_versions.score == "v1"


def test_sam_state_records_and_reads_requests_per_day():
    sam = SamState()
    today = date(2026, 9, 24)
    assert sam.requests_today(today) == 0
    sam.record_request(today)
    sam.record_request(today, count=2)
    assert sam.requests_today(today) == 3
    assert sam.requests_today(date(2026, 9, 23)) == 0


def test_save_and_load_round_trip(tmp_path):
    state = GrantsState(profile_hash="abc123")
    state.sam.last_posted_to = date(2026, 9, 20)
    state.sam.record_request(date(2026, 9, 24), count=3)

    path = tmp_path / "state.json"
    save_state(state, path)
    loaded = load_state(path)

    assert loaded.profile_hash == "abc123"
    assert loaded.sam.last_posted_to == date(2026, 9, 20)
    assert loaded.sam.requests_today(date(2026, 9, 24)) == 3


def test_load_state_missing_file_returns_default(tmp_path):
    loaded = load_state(tmp_path / "does_not_exist.json")
    assert loaded == GrantsState()


def test_profile_changed_detects_difference():
    state = GrantsState(profile_hash="old-hash")
    assert profile_changed(state, "new-hash") is True
    assert profile_changed(state, "old-hash") is False


def test_profile_changed_false_when_no_prior_hash():
    state = GrantsState(profile_hash=None)
    assert profile_changed(state, "any-hash") is False
