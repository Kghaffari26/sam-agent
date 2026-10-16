"""Label review (scope S2): the sheet's order and applying a filled review."""

import json
import re

import pytest

from evals.grants import labels


def fill(md: str, marks: dict[str, str], default: str = "✓") -> str:
    """Put a mark in the last column of every table row (by id, else `default`)."""
    out = []
    for line in md.splitlines():
        m = re.match(r"^\|\s*(R?\d+)\s*\|\s*(?:⚠\s*)?([\w:.\-]+)\s*\|", line)
        if m:
            line = line.rstrip()[:-1].rstrip()
            line = line[: line.rfind("|") + 1] + f" {marks.get(m.group(2), default)} |"
        out.append(line)
    return "\n".join(out) + "\n"


@pytest.fixture
def tmp_files(tmp_path, monkeypatch):
    cases = tmp_path / "cases.json"
    cases.write_text(labels.RESEARCH_CASES.read_text())
    monkeypatch.setattr(labels, "RESEARCH_CASES", cases)
    monkeypatch.setattr(labels, "REVIEWED", tmp_path / "labels_reviewed.json")
    return tmp_path


def test_sheet_lists_r3_first_and_disagreements_first():
    md = labels.REVIEW_MD.read_text()
    scoring, research = labels.parse_review(md)
    assert next(iter(research)) == "r3-nsf-grant" and len(research) == 5
    assert set(scoring) == set(labels.proposed()) and len(scoring) == 40
    rows = [line for line in md.splitlines() if re.match(r"^\| \d+ \|", line)]
    flagged = [i for i, r in enumerate(rows) if "⚠" in r]
    assert flagged == list(range(len(flagged)))  # all flagged rows come first
    assert "**go**" in md and "**no_go**" in md


def test_apply_writes_reviewed_labels_and_research_corrections(tmp_files):
    md = fill(labels.REVIEW_MD.read_text(), {"sam:m008": "good_fit", "r3-nsf-grant": "go"})
    changed = labels.apply_review(md)
    assert changed == {"scoring_changed": {"sam:m008": ("maybe", "good_fit")},
                       "research_changed": {"r3-nsf-grant": ("no_go", "go")}}
    reviewed = json.loads(labels.REVIEWED.read_text())["labels"]
    assert len(reviewed) == 40
    assert reviewed["sam:m008"] == {"label": "good_fit", "proposed": "maybe",
                                    "reason": labels.proposed()["sam:m008"]["reason"]}
    r3 = next(c for c in labels.research_cases() if c["id"] == "r3-nsf-grant")
    assert r3["expected"]["go_no_go"] == "go"


def test_apply_refuses_an_incomplete_review(tmp_files):
    md = fill(labels.REVIEW_MD.read_text(), {"gg:g003": "", "sam:m001": "great"})
    with pytest.raises(ValueError, match="gg:g003: blank.*sam:m001"):
        labels.apply_review(md)
    assert not labels.REVIEWED.exists()


def test_research_cases_round_trip_byte_for_byte(tmp_files):
    before = labels.RESEARCH_CASES.read_text()
    labels.apply_review(fill(labels.REVIEW_MD.read_text(), {"r1-va-dashboard": "no_go"}))
    after = labels.RESEARCH_CASES.read_text()
    assert after != before
    assert after.replace('"no_go"', '"go"', 1) == before  # only r1's label changed


def test_suites_read_reviewed_labels_once_they_exist(tmp_files):
    assert labels.load_labels()[1] is False
    labels.apply_review(fill(labels.REVIEW_MD.read_text(), {}))
    loaded, reviewed = labels.load_labels()
    assert reviewed and loaded.keys() == labels.proposed().keys()
