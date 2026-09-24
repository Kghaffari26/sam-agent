import pytest

from agents.grants.eligibility import (
    GRANTS_GOV_APPLICANT_TYPES,
    is_eligible_for_grant,
    label_for,
)


def test_no_codes_means_unrestricted():
    assert is_eligible_for_grant([], "small_business") is True


def test_unrestricted_code_passes_any_entity_type():
    for entity_type in ["small_business", "nonprofit", "university", "individual", "large_business"]:
        assert is_eligible_for_grant(["99"], entity_type) is True


@pytest.mark.parametrize(
    "entity_type,matching_code,other_code",
    [
        ("small_business", "23", "22"),
        ("nonprofit", "12", "23"),
        ("nonprofit", "13", "23"),
        ("university", "06", "23"),
        ("university", "20", "23"),
        ("individual", "21", "23"),
        ("large_business", "22", "23"),
    ],
)
def test_entity_type_matches_its_codes(entity_type, matching_code, other_code):
    assert is_eligible_for_grant([matching_code], entity_type) is True
    assert is_eligible_for_grant([other_code], entity_type) is False


def test_multiple_codes_match_if_any_fits():
    assert is_eligible_for_grant(["22", "23"], "small_business") is True


def test_every_code_has_a_label():
    for code in GRANTS_GOV_APPLICANT_TYPES:
        assert label_for(code)
    assert label_for("NOT_A_REAL_CODE") is None
