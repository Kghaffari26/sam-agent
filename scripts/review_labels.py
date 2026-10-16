"""Eval label review (scope S2).

    uv run python -m scripts.review_labels                        # write labels_review.md
    uv run python -m scripts.review_labels --refresh-model-view   # re-score the 40 items
                                                                  #   first (~$0.03)
    uv run python -m scripts.review_labels --apply                # after the review

`labels_review.md` has one row per label (title, agency, closing date, proposed label,
the model's view, the one-line reason), the rows the model disagrees with first, and
the research go/no-go labels with `r3-nsf-grant` at the top. `--apply` checks every
row is marked, writes `labels_reviewed.json` (which the evals read from then on) and
applies go/no-go corrections to `research/cases.json`.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from evals.grants import labels


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--refresh-model-view", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--max-usd", type=float, default=0.10)
    args = ap.parse_args(argv)

    if args.apply:
        result = labels.apply_review(labels.REVIEW_MD.read_text())
        print(f"wrote {labels.REVIEWED}")
        for kind, changed in result.items():
            for id_, (old, new) in changed.items():
                print(f"  {kind}: {id_} {old} -> {new}")
        return 0

    if args.refresh_model_view:
        from agents_core import settings
        from agents_core.costs import CostTracker
        from agents_core.llm import LLM

        now = datetime.now(UTC)
        tracker = CostTracker(agent="eval:grants-label-review",
                              run_id=now.strftime("%Y-%m-%dT%H-%M-%SZ"), max_usd=args.max_usd,
                              path=settings.data_dir() / "eval_costs.jsonl")
        labels.record_model_view(LLM(tracker))
        print(f"model view recorded (${tracker.total_usd:.4f}) -> {labels.MODEL_VIEW}")

    md = labels.write_review()
    n = md.count("| ⚠ ")
    print(f"{labels.REVIEW_MD}: {n} disagreements flagged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
