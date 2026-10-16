"""Fill the S1 calibration sheet and the S2 label review from the owner's answers on
the review page (a claude.ai artifact whose store holds `scores/<item_id>` =
{score, notes} and `labels/<id>` = {mark}).

    uv run python -m scripts.import_review DIR

DIR holds the store exported as JSON files (`DIR/scores/<id>.json`,
`DIR/labels/<id>.json`, each a document `{"id": ..., "data": {...}}` or the bare
data object). Writes `evals/grants/calibration/sheet.csv` (score and notes columns)
and the review column of `evals/grants/labels_review.md`; then run
`scripts.calibrate_judges` and `scripts.review_labels --apply`.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

from evals.grants import calibration as cal
from evals.grants import labels


def read_dir(path: Path) -> dict[str, dict]:
    out = {}
    for f in sorted(path.glob("*.json")):
        doc = json.loads(f.read_text())
        data = doc.get("data", doc) if isinstance(doc, dict) else {}
        out[doc.get("id", f.stem) if isinstance(doc, dict) else f.stem] = data
    return out


def fill_sheet(scores: dict[str, dict], path: Path = cal.SHEET_CSV) -> int:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    n = 0
    for row in rows:
        s = scores.get(row["item_id"])
        if s and s.get("score"):
            row[cal.SCORE_COLUMN] = str(int(s["score"]))
            row["notes"] = s.get("notes") or ""
            n += 1
    cal.write_csv(rows, path)
    return n


_ROW = re.compile(r"^(\|\s*(R?\d+)\s*\|\s*(?:⚠\s*)?([\w:.\-]+)\s*\|.*\|)\s*[^|]*\|\s*$")


def fill_review(marks: dict[str, dict], path: Path = labels.REVIEW_MD) -> int:
    out, n = [], 0
    for line in path.read_text().splitlines():
        m = _ROW.match(line)
        if m and marks.get(m.group(3), {}).get("mark"):
            line = f"{m.group(1)} {marks[m.group(3)]['mark']} |"
            n += 1
        out.append(line)
    path.write_text("\n".join(out) + "\n")
    return n


def main(argv: list[str] | None = None) -> int:
    root = Path((argv or sys.argv[1:])[0])
    n_s = fill_sheet(read_dir(root / "scores"))
    n_l = fill_review(read_dir(root / "labels"))
    print(f"{n_s}/20 scores -> {cal.SHEET_CSV}; {n_l}/45 label marks -> {labels.REVIEW_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
