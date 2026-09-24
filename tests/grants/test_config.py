from pathlib import Path

import pytest
from pydantic import ValidationError

from agents.grants.config import (
    BusinessProfile,
    load_business_profile,
    load_grants_config,
    profile_hash,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_loads_example_business_profile():
    profile = load_business_profile(REPO_ROOT / "config" / "business_profile.toml")
    assert profile.id == "default"
    assert profile.entity_type == "small_business"
    assert "541511" in profile.naics_primary
    assert profile.min_value == 25000
    assert profile.max_value == 5000000


def test_loads_grants_toml():
    config = load_grants_config(REPO_ROOT / "config" / "grants.toml")
    assert config.settings.sam_daily_request_budget == 8
    assert config.settings.max_sam_description_fetches == 3
    assert config.recommendation.pursue == 75
    assert config.recommendation.consider == 55
    assert config.sam.ptypes == ["o", "k", "p", "r"]
    assert config.grants_gov.opp_statuses == "forecasted|posted"


def test_bad_profile_fails_fast():
    with pytest.raises(ValidationError):
        BusinessProfile.model_validate({"id": "bad", "name": "x", "summary": "x"})


def test_bad_entity_type_fails_fast():
    with pytest.raises(ValidationError):
        BusinessProfile.model_validate(
            {
                "id": "bad",
                "name": "x",
                "summary": "x",
                "entity_type": "not_a_real_type",
            }
        )


def test_profile_hash_stable_for_same_content():
    profile = load_business_profile(REPO_ROOT / "config" / "business_profile.toml")
    assert profile_hash(profile) == profile_hash(profile.model_copy())


def test_profile_hash_ignores_formatting_only_changes(tmp_path):
    original = REPO_ROOT / "config" / "business_profile.toml"
    reformatted = tmp_path / "business_profile.toml"
    text = original.read_text()
    # Same semantic content, different whitespace/formatting.
    reformatted.write_text(text.replace("  ", " "))

    assert profile_hash(load_business_profile(original)) == profile_hash(
        load_business_profile(reformatted)
    )


def test_profile_hash_changes_when_content_changes(tmp_path):
    original = REPO_ROOT / "config" / "business_profile.toml"
    edited = tmp_path / "business_profile.toml"
    edited.write_text(original.read_text().replace('id = "default"', 'id = "changed"'))

    assert profile_hash(load_business_profile(original)) != profile_hash(
        load_business_profile(edited)
    )
