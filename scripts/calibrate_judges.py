"""Calibrate the summary and research LLM judges against the owner's scores (scope S1).

    uv run python -m scripts.calibrate_judges [--sheet PATH] [--max-usd 0.30]

Reads the filled `evals/grants/calibration/sheet.csv` (every row scored 1-5) and the
frozen `items.json`, runs agents-core's `LLMJudge.calibrate` with each suite's judge
(20 fast-tier calls, about $0.03), and writes:

- `calibration/calibration.json`: per judge rho, MAE, bias, the threshold mapping and
  `calibrated` (rho >= 0.6); `suites.py` takes the judges' pass thresholds from it;
- `calibration/report.md`: every run so far (before/after a rubric revision);
- one `grants-judge-calibration` line in `evals/history.jsonl` (`calibrated`, `rho`).

Spend goes to `data/eval_costs.jsonl` under `eval:grants-judge-calibration`.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from agents_core import settings
from agents_core.costs import CostTracker
from agents_core.llm import LLM, tier_config

from evals.grants import calibration as cal
from evals.grants import suites


def judges():
    def find(suite):
        return next(s for s in suite.scorers if getattr(s, "name", "") == "judge_quality")

    return {"summaries": find(suites.SUMMARIES), "research": find(suites.RESEARCH)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sheet", type=Path, default=cal.SHEET_CSV)
    ap.add_argument("--max-usd", type=float, default=0.30)
    args = ap.parse_args(argv)

    items = json.loads(cal.ITEMS_PATH.read_text())
    scores = cal.read_scores(args.sheet)
    missing = [it["item_id"] for it in items if it["item_id"] not in scores]
    if missing:
        raise SystemExit(f"no score for {missing}")

    now = datetime.now(UTC)
    tracker = CostTracker(agent="eval:grants-judge-calibration",
                          run_id=now.strftime("%Y-%m-%dT%H-%M-%SZ"), max_usd=args.max_usd,
                          path=settings.data_dir() / "eval_costs.jsonl")
    llm = LLM(tracker)
    kind_of = {"summaries": "summary", "research": "research"}
    run = {name: cal.calibrate_kind(llm, judge, items, scores, kind_of[name])
           for name, judge in judges().items()}
    run.update(ts=now.strftime("%Y-%m-%dT%H:%M:%SZ"), rubric_version=suites.JUDGE_RUBRIC_VERSION,
               usd=round(tracker.total_usd, 6), model=tier_config("fast").model)

    previous = json.loads(cal.CALIBRATION_JSON.read_text()) if cal.CALIBRATION_JSON.is_file() \
        else {"runs": []}
    runs = [*previous["runs"], run]
    cal.CALIBRATION_JSON.write_text(json.dumps(
        {"summaries": run["summaries"], "research": run["research"], "runs": runs},
        indent=1) + "\n")
    cal.REPORT_MD.write_text(cal.report_markdown(runs))
    history = settings.evals_dir() / "history.jsonl"
    with history.open("a") as f:
        f.write(json.dumps(cal.history_line(run, now=now)) + "\n")
    for name in ("summaries", "research"):
        v = run[name]
        print(f"{name}: rho={v['rho']} mae={v['mae']} threshold={v['pass_threshold']} "
              f"calibrated={v['calibrated']}")
    print(f"${run['usd']:.4f} -> {cal.REPORT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
