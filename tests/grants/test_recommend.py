"""Contractor setup: intake -> recommended profile, filters and scoring (SPEC §2.1)."""

import json
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from agents.grants.config import DEFAULT_WEIGHTS, BusinessProfile, load_business_profile
from agents.grants.recommend import (
    CAPABILITY_FLOOR,
    ContractorIntake,
    match_trades,
    recommend_profile,
    to_toml,
)
from tools import setup_contractor

INTAKES = Path(__file__).resolve().parents[2] / "config" / "intakes"


def intake(**kw) -> ContractorIntake:
    base = dict(company_name="Acme Co", description="We build web applications and data "
                "pipelines in the cloud.", services=["software development"])
    base.update(kw)
    return ContractorIntake(**base)


def test_example_intakes_produce_valid_profiles():
    for path in INTAKES.glob("*.json"):
        rec = recommend_profile(setup_contractor.load_intake(path))
        BusinessProfile.model_validate(rec.profile.model_dump())
        assert rec.trades


def test_matches_trades_from_the_description():
    hvac = intake(description="Commercial HVAC and plumbing service; chillers and boilers.",
                  services=[])
    assert [t.id for t, _ in match_trades(hvac)][0] == "hvac_plumbing"
    guards = intake(description="Armed guard and unarmed guard services, mobile patrol.",
                    services=[])
    assert [t.id for t, _ in match_trades(guards)][0] == "security_guard"


def test_shared_naics_alone_does_not_add_trades():
    # 541512 is in both software and cybersecurity: the description decides.
    rec = recommend_profile(intake(naics=["541512"]))
    assert rec.trades == ["Software and IT services"]
    assert rec.profile.naics_primary == ["541512"]


def test_naics_decides_when_the_description_names_no_trade():
    rec = recommend_profile(intake(description="Family-owned firm.", services=[],
                                   naics=["561720"]))
    assert rec.trades == ["Janitorial, grounds and facilities"]


def test_unrecognized_trade_still_yields_a_profile():
    rec = recommend_profile(intake(description="We do things.", services=[]))
    assert rec.trades == [] and rec.profile.naics_primary == []
    assert "add your NAICS" in rec.rationale["naics_primary"]


def test_remote_and_states():
    software = recommend_profile(intake(states=["VA"]))
    assert software.profile.remote_ok and software.profile.states == []
    hvac = recommend_profile(intake(description="HVAC repair contractor", services=[],
                                    states=["fl"]))
    assert not hvac.profile.remote_ok and hvac.profile.states == ["FL"]


def test_negative_keywords_never_block_own_work():
    rec = recommend_profile(intake(description="HVAC and construction renovation firm",
                                   services=["renovation"], avoid=["janitorial"]))
    negatives = [n.lower() for n in rec.profile.negative_keywords]
    assert "janitorial" in negatives
    assert "construction" not in negatives and "renovation" not in negatives
    assert "hvac" not in negatives
    assert "software development" in negatives


def test_value_range_sources():
    assert (recommend_profile(intake(typical_contract_min=5e4, typical_contract_max=2e6))
            .profile.max_value) == 2e6
    by_revenue = recommend_profile(intake(annual_revenue=2_000_000)).profile
    assert (by_revenue.min_value, by_revenue.max_value) == (20_000, 3_000_000)
    assert recommend_profile(intake(employees=5)).profile.max_value == 1_000_000
    assert recommend_profile(intake()).profile.max_value is None


def test_selective_strategy_tightens_filters_and_bands():
    rec = recommend_profile(intake(bid_strategy="selective", certifications=["SDVOSB"],
                                   past_performance=["A prior federal job"]))
    f, s = rec.profile.filters, rec.profile.scoring
    assert f.naics == ["5415", "5182"]
    assert f.set_asides == ["SBA", "SBP", "SDVOSBS"]  # no sole-source codes
    assert f.exclude_sole_source
    assert rec.profile.notice_types == ["solicitation", "combined_synopsis_solicitation"]
    assert (s.pursue, s.consider, s.set_aside_bonus) == (80, 60, 5)


def test_volume_and_balanced_bands():
    assert recommend_profile(intake(bid_strategy="volume")).profile.scoring.pursue == 70
    balanced = recommend_profile(intake()).profile
    assert balanced.scoring.pursue is None and balanced.filters.naics == []
    assert "sources_sought" in balanced.notice_types


def test_weights_sum_to_100_and_keep_capability_first():
    rich = recommend_profile(intake(employees=4, certifications=["8(a)"],
                                    target_agencies=["GSA"])).profile.scoring.weights
    assert sum(rich.values()) == 100
    assert rich["capability"] >= CAPABILITY_FLOOR
    assert rich["capability"] == max(rich.values())
    established = recommend_profile(intake(past_performance=["x"])).profile.scoring.weights
    assert established == DEFAULT_WEIGHTS


def test_certifications_are_normalized_and_checked():
    assert intake(certifications=["8(a)", "hubzone", "8a"]).certifications == ["8A", "HUBZone"]
    with pytest.raises(ValidationError):
        intake(certifications=["ISO 9001"])


def test_grants_inferred_from_entity_type_and_research():
    assert recommend_profile(intake(entity_type="nonprofit")).profile.include_grants
    assert not recommend_profile(intake()).profile.include_grants
    rd = recommend_profile(intake(description="SBIR research and prototype development"))
    assert rd.profile.include_grants and "SBIR" in rd.profile.grant_keywords


def test_toml_round_trips_with_reasons_as_comments(tmp_path):
    rec = recommend_profile(intake(bid_strategy="selective", employees=12))
    text = to_toml(rec)
    assert "# Sole-source and intent-to-award notices" in text
    path = tmp_path / "p.toml"
    path.write_text(text)
    assert load_business_profile(path) == rec.profile
    assert tomllib.loads(text)["scoring"]["weights"] == rec.profile.scoring.weights


def test_cli_writes_profile_json_and_schema(tmp_path, capsys):
    src = tmp_path / "intake.json"
    src.write_text(intake().model_dump_json())
    out = tmp_path / "profiles" / "acme.toml"
    grants = tmp_path / "grants.toml"
    grants.write_text('[settings]\nprofile = "config/business_profile.toml"\n')

    assert setup_contractor.main([str(src), "--out", str(out)]) == 0
    assert load_business_profile(out).id == "acme-co"
    setup_contractor.activate(out, grants)
    assert tomllib.loads(grants.read_text())["settings"]["profile"] == out.as_posix()

    capsys.readouterr()
    assert setup_contractor.main([str(src), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"profile", "trades", "rationale"}
    assert setup_contractor.main(["--schema"]) == 0
    assert "company_name" in json.loads(capsys.readouterr().out)["properties"]
