import pytest

from agents.grants.config import BusinessProfile
from agents.grants.setasides import SET_ASIDES, is_eligible_for_set_aside, label_for


def make_profile(**overrides) -> BusinessProfile:
    base = dict(
        id="test",
        name="Test co",
        summary="test",
        entity_type="small_business",
        certifications=[],
    )
    base.update(overrides)
    return BusinessProfile.model_validate(base)


def test_no_set_aside_is_always_eligible():
    assert is_eligible_for_set_aside(None, make_profile()) is True


def test_unknown_code_is_not_a_blocker():
    assert is_eligible_for_set_aside("NOT_A_REAL_CODE", make_profile()) is True


@pytest.mark.parametrize("code", ["SBA", "SBP"])
def test_small_business_set_aside_requires_small_business_entity_type(code):
    assert is_eligible_for_set_aside(code, make_profile(entity_type="small_business")) is True
    assert is_eligible_for_set_aside(code, make_profile(entity_type="large_business")) is False


@pytest.mark.parametrize(
    "code,cert",
    [
        ("8A", "8A"),
        ("8AN", "8A"),
        ("HZC", "HUBZone"),
        ("HZS", "HUBZone"),
        ("SDVOSBC", "SDVOSB"),
        ("SDVOSBS", "SDVOSB"),
        ("WOSB", "WOSB"),
        ("WOSBSS", "WOSB"),
        ("EDWOSB", "EDWOSB"),
        ("EDWOSBSS", "EDWOSB"),
        ("VSA", "VOSB"),
        ("VSS", "VOSB"),
    ],
)
def test_certification_gated_set_asides(code, cert):
    assert is_eligible_for_set_aside(code, make_profile(certifications=[])) is False
    assert is_eligible_for_set_aside(code, make_profile(certifications=[cert])) is True


def test_every_set_aside_has_a_label():
    for code, info in SET_ASIDES.items():
        assert info.label
        assert label_for(code) == info.label


def test_label_for_unknown_code_is_none():
    assert label_for("NOT_A_REAL_CODE") is None
    assert label_for(None) is None
