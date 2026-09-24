import json
from pathlib import Path

from agents.grants.models import Opportunity

EVALS_DIR = Path(__file__).resolve().parents[2] / "evals" / "grants"


def test_dataset_has_40_valid_opportunities():
    records = json.loads((EVALS_DIR / "dataset.json").read_text())
    assert len(records) == 40
    ids = set()
    for record in records:
        opp = Opportunity.model_validate(record)
        ids.add(opp.id)
    assert len(ids) == 40  # all ids unique


def test_labels_cover_every_dataset_id_with_a_reason():
    records = json.loads((EVALS_DIR / "dataset.json").read_text())
    labels = json.loads((EVALS_DIR / "labels_proposed.json").read_text())["labels"]

    record_ids = {r["id"] for r in records}
    assert set(labels.keys()) == record_ids

    for entry in labels.values():
        assert entry["label"] in ("good_fit", "maybe", "bad_fit")
        assert entry["reason"].strip()


def test_labels_file_is_marked_provisional():
    data = json.loads((EVALS_DIR / "labels_proposed.json").read_text())
    assert "PROVISIONAL" in data["note"]
