"""Judge calibration (scope S1): the frozen sheet, reading the owner's scores,
Spearman's rho, the threshold mapping and the calibrated pass thresholds."""

import csv
import json
from types import SimpleNamespace

import pytest
from agents_core.costs import CostTracker
from agents_core.evals import LLMJudge
from agents_core.llm import LLM

from evals.grants import calibration as cal


def test_sheet_has_twenty_items_and_no_judge_scores():
    items = json.loads(cal.ITEMS_PATH.read_text())
    assert len(items) == 20
    kinds = [(it["kind"], it["source"]) for it in items]
    assert kinds.count(("summary", "live")) == 10
    assert kinds.count(("research", "live")) == 5
    assert kinds.count(("research", "fixture")) == 5
    with cal.SHEET_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["item_id"] for r in rows] == [it["item_id"] for it in items]
    assert set(rows[0]) == set(cal.CSV_COLUMNS)
    for row in rows:
        assert row["rubric"] and row["output"] and row["opportunity"]
        assert "judge" not in row["output"].lower()
    html = cal.SHEET_HTML.read_text()
    assert html.count('class="item"') == 20 and "judge_quality" not in html


def test_sheet_rows_render_both_kinds():
    items = json.loads(cal.ITEMS_PATH.read_text())
    rows = cal.sheet_rows(items)
    research = next(r for r in rows if r["kind"] == "research")
    assert "Go/no-go:" in research["output"] and "Rubric fit:" in research["opportunity"]
    summary = next(r for r in rows if r["kind"] == "summary")
    assert "Next steps:" in summary["output"] and summary[cal.SCORE_COLUMN] == ""


def _write_sheet(path, scores):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["item_id", cal.SCORE_COLUMN])
        w.writeheader()
        for item_id, score in scores.items():
            w.writerow({"item_id": item_id, cal.SCORE_COLUMN: score})


def test_read_scores_rejects_blank_and_out_of_range(tmp_path):
    path = tmp_path / "sheet.csv"
    _write_sheet(path, {"c01": "5", "c02": " 3 ", "c03": "4.0"})
    assert cal.read_scores(path) == {"c01": 5, "c02": 3, "c03": 4}
    _write_sheet(path, {"c01": "", "c02": "6", "c03": "2"})
    with pytest.raises(ValueError, match="2 rows"):
        cal.read_scores(path)


def test_spearman_handles_ties_and_constant_input():
    assert cal.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert cal.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert cal.spearman([1, 1, 2, 3], [1, 2, 3, 4]) == pytest.approx(0.9487, abs=1e-3)
    assert cal.spearman([1, 1, 1], [1, 2, 3]) is None


def test_threshold_mapping_picks_the_best_agreeing_threshold():
    # A lenient judge: says 4/5 (0.75) where the human says 3/5 (0.5).
    pairs = [(0.75, 0.5), (0.75, 0.5), (1.0, 0.75), (1.0, 1.0), (0.5, 0.25)]
    m = cal.threshold_mapping(pairs)
    assert m["pass_threshold"] == 1.0
    assert m["agreement_by_threshold"]["1.00"] == 1.0
    assert m["agreement_by_threshold"]["0.75"] == 0.6
    assert m["human_mean_by_judge_score"] == {"3": 2.0, "4": 3.0, "5": 4.5}


def test_threshold_mapping_ties_prefer_the_default():
    pairs = [(1.0, 1.0), (0.0, 0.0)]
    assert cal.threshold_mapping(pairs)["pass_threshold"] == 0.75


class JudgeClient:
    """Answers LLMJudge's structured call with a score chosen from the prompt."""

    def __init__(self, score_for):
        self.score_for = score_for
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, output_format, **params):
        prompt = params["messages"][0]["content"]
        data = {"reasoning": "ok", "score": self.score_for(prompt)}
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(data))],
            parsed_output=output_format.model_validate(data),
            usage=SimpleNamespace(input_tokens=500, output_tokens=40,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=0),
            stop_reason="end_turn",
        )


def _items(n=6):
    return [{"item_id": f"c{i:02d}", "kind": "summary", "source": "live", "title": f"T{i}",
             "input": {"title": f"T{i}"}, "output": {"text": f"marker-{i}"}}
            for i in range(1, n + 1)]


def test_calibrate_kind_reports_rho_mae_and_disagreements(tmp_path):
    human = {"c01": 5, "c02": 4, "c03": 4, "c04": 3, "c05": 2, "c06": 1}
    judge_scores = {1: 5, 2: 5, 3: 4, 4: 4, 5: 2, 6: 3}

    def score_for(prompt):
        return next(v for k, v in judge_scores.items() if f"marker-{k}\"" in prompt)

    tracker = CostTracker(agent="t", run_id="t", max_usd=1.0, path=tmp_path / "c.jsonl")
    llm = LLM(tracker, client=JudgeClient(score_for), sleep=lambda s: None)
    out = cal.calibrate_kind(llm, LLMJudge("rubric"), _items(), human, "summary")
    assert out["n"] == 6
    assert out["rho"] == pytest.approx(cal.spearman([5, 5, 4, 4, 2, 3], [5, 4, 4, 3, 2, 1]),
                                       abs=1e-3)
    assert out["mae"] == pytest.approx((0 + 1 + 0 + 1 + 0 + 2) / 6 / 4, abs=1e-3)
    assert out["calibrated"] is True
    assert out["largest_disagreements"][0] == {
        "item_id": "c06", "title": "T6", "source": "live", "judge": 3, "human": 1}


def test_pass_threshold_only_when_calibrated(tmp_path):
    path = tmp_path / "calibration.json"
    assert cal.pass_threshold("summaries", path) == 0.75
    path.write_text(json.dumps({"summaries": {"calibrated": True, "pass_threshold": 1.0},
                                "research": {"calibrated": False, "pass_threshold": 0.5}}))
    assert cal.pass_threshold("summaries", path) == 1.0
    assert cal.pass_threshold("research", path) == 0.75


def test_history_line_records_calibrated_and_rho():
    v = {"n": 10, "rho": 0.71, "mae": 0.15, "pass_threshold": 0.75, "calibrated": True}
    line = cal.history_line({"summaries": v, "research": {**v, "rho": 0.5, "calibrated": False},
                             "rubric_version": "j1", "model": "m", "usd": 0.03})
    assert line["suite"] == "grants-judge-calibration"
    assert line["calibrated"] is False
    assert line["rho"] == {"summaries": 0.71, "research": 0.5}
    assert line["scores"]["summaries_rho"] == 0.71 and line["n_scored"] == 20
