"""Write the judge calibration sheet (scope S1): 10 published summaries, the 5 live
bid research briefs and 5 replayed fixture briefs, each with its rubric and output
and an empty 1-5 score column. No judge scores are shown.

    uv run python -m scripts.make_calibration_sheet

Writes `evals/grants/calibration/items.json` (the frozen items), `sheet.csv` and
`sheet.html` (the same items, with radio buttons and a Download CSV button). No
network, no LLM calls.
"""

from __future__ import annotations

from evals.grants import calibration


def main() -> None:
    items = calibration.make_sheet()
    kinds = [f"{sum(it['kind'] == k and it['source'] == s for it in items)} {s} {k}"
             for k, s in (("summary", "live"), ("research", "live"), ("research", "fixture"))]
    print(f"{len(items)} items ({', '.join(kinds)}) -> {calibration.SHEET_CSV}, "
          f"{calibration.SHEET_HTML}")


if __name__ == "__main__":
    main()
