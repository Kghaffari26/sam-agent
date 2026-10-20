"""Eval labels and their human review (scope S2).

`labels_proposed.json` holds the 40 scoring labels Claude proposed (kept for
history). `labels_review.md` is the review sheet: one row per label, the rows the
current model disagrees with first, plus the five research go/no-go labels
(`research/cases.json`) with `r3-nsf-grant` at the very top. Once the owner has
marked every row, `apply_review` writes `labels_reviewed.json` (which `suites.py`
reads from then on) and applies any go/no-go corrections to `research/cases.json`.

The model's view (`labels_model_view.json`) is one production scoring of the 40
items, recorded by `scripts/review_labels.py --refresh-model-view` (~$0.03) so the
sheet can be rebuilt without spend.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROPOSED = HERE / "labels_proposed.json"
REVIEWED = HERE / "labels_reviewed.json"
MODEL_VIEW = HERE / "labels_model_view.json"
REVIEW_MD = HERE / "labels_review.md"
RESEARCH_CASES = HERE / "research" / "cases.json"

SCORING_LABELS = ("good_fit", "maybe", "bad_fit")
RESEARCH_LABELS = ("go", "no_go")
# The rubric's bands map onto the labels (a hard-filter rejection is a bad fit).
LABEL_FOR_RECOMMENDATION = {"Pursue": "good_fit", "Consider": "maybe", "Pass": "bad_fit"}
OK_MARKS = ("✓", "✔", "ok", "y", "yes")

# Both sides of the one open research call, for the reviewer.
R3_ARGUMENTS = {
    "go": "An NSF award for applied AI/data tooling is squarely in the firm's technical "
          "lane; small businesses are eligible and the scope is software, so a "
          "well-targeted proposal is a credible first grant.",
    "no_go": "A competitive NSF research grant favours applicants with a research track "
             "record and institutional partners; a five-person services firm with no "
             "grant history is unlikely to win, and the proposal effort is large. "
             "(The model's call; the owner chose this on 2026-10-16.)",
}


def proposed() -> dict[str, dict[str, str]]:
    return json.loads(PROPOSED.read_text())["labels"]


def load_labels() -> tuple[dict[str, dict[str, str]], bool]:
    """The scoring labels the evals use, and whether they are human-reviewed."""
    if REVIEWED.is_file():
        return json.loads(REVIEWED.read_text())["labels"], True
    return proposed(), False


def dataset() -> dict[str, dict[str, Any]]:
    return {o["id"]: o for o in json.loads((HERE / "dataset.json").read_text())}


def research_cases() -> list[dict[str, Any]]:
    return json.loads(RESEARCH_CASES.read_text())


# ---- the model's view -------------------------------------------------------------


def model_view_from_scores(scores: dict[str, Any], rejected: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for id_, reason in rejected.items():
        out[id_] = {"label": "bad_fit", "fit": None, "recommendation": None,
                    "why": f"hard filter: {reason}"}
    for id_, s in scores.items():
        out[id_] = {"label": LABEL_FOR_RECOMMENDATION[s.recommendation], "fit": s.fit,
                    "recommendation": s.recommendation,
                    "why": "; ".join([*s.reasons[:1], *s.red_flags[:1]])}
    return out


def record_model_view(llm) -> dict[str, Any]:
    """Score the labeled set once with the production scorer (Batch API) and save it."""
    from agents.grants.config import profile_hash
    from agents.grants.filters import check_hard_filters
    from agents.grants.scoring import score_opportunities
    from evals.grants.suites import AS_OF, NOW, SCORE_PROMPT_VERSION, _profile_and_config
    from evals.grants.suites import load_dataset as opportunities

    profile, config = _profile_and_config()
    survivors, rejected = [], {}
    for opp in opportunities():
        reason = check_hard_filters(opp, profile, today=AS_OF)
        if reason is None:
            survivors.append(opp)
        else:
            rejected[opp.id] = reason
    run = score_opportunities(llm, survivors, profile, config,
                              profile_hash=profile_hash(profile), today=AS_OF, now=NOW)
    s = {"a": run.scores, "rejected": rejected}
    view = {
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prompt_version": SCORE_PROMPT_VERSION,
        "items": model_view_from_scores(s["a"], s["rejected"]),
    }
    MODEL_VIEW.write_text(json.dumps(view, indent=1) + "\n")
    return view


# ---- the review sheet -------------------------------------------------------------


def _cell(text: Any) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def _closes(deadline: str | None) -> str:
    return deadline[:10] if deadline else "—"


def _model_cell(m: dict[str, Any] | None) -> str:
    if not m:
        return "—"
    if m["recommendation"] is None:
        return f"bad_fit ({m['why']})"
    return f"{m['label']} ({m['recommendation']} {m['fit']})"


def scoring_rows(labels: dict[str, dict[str, str]], view: dict[str, Any],
                 data: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per label; disagreements first (then by id)."""
    rows = []
    for id_, lab in labels.items():
        opp = data[id_]
        m = view.get(id_)
        rows.append({
            "id": id_, "title": opp["title"], "agency": opp.get("agency") or "—",
            "closes": _closes(opp.get("deadline")), "proposed": lab["label"],
            "reason": lab["reason"], "model": _model_cell(m),
            "disagrees": bool(m) and m["label"] != lab["label"],
        })
    rows.sort(key=lambda r: (not r["disagrees"], r["id"]))
    return rows


def research_rows(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for c in cases:
        opp = c["input"]["opportunity"]
        rows.append({"id": c["id"], "title": opp["title"], "agency": opp.get("agency") or "—",
                     "closes": _closes(opp.get("deadline")),
                     "proposed": c["expected"]["go_no_go"]})
    rows.sort(key=lambda r: (r["id"] != "r3-nsf-grant", r["id"]))
    return rows


HEADER = "| # | id | title | agency | closes | proposed | model | reason | review |"
RESEARCH_HEADER = "| # | id | title | agency | closes | proposed | review |"


def review_markdown(scoring: list[dict[str, Any]], research: list[dict[str, Any]],
                    *, model_recorded_at: str | None, today: date) -> str:
    n_dis = sum(r["disagrees"] for r in scoring)
    lines = [
        "# Eval label review",
        "",
        f"_Generated {today.isoformat()} by `scripts/review_labels.py`; the model column is"
        f" one production scoring recorded {model_recorded_at or '(not recorded)'}._",
        "",
        "**How to review:** in the last column of every row, put `✓` to keep the proposed"
        " label, or write the corrected label (`good_fit` / `maybe` / `bad_fit`, or `go` /"
        " `no_go` for research). Leave nothing blank. Then run"
        " `uv run python -m scripts.review_labels --apply` (or ask Claude to).",
        "",
        "Model labels map from the rubric bands: Pursue → good_fit, Consider → maybe,"
        " Pass or a hard-filter rejection → bad_fit.",
        "",
        "## Research go/no-go labels (`research/cases.json`)",
        "",
        "`r3-nsf-grant` first. Both arguments:",
        "",
        f"- **go**: {R3_ARGUMENTS['go']}",
        f"- **no_go**: {R3_ARGUMENTS['no_go']}",
        "",
        RESEARCH_HEADER,
        "|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(research, start=1):
        lines.append(f"| R{i} | {r['id']} | {_cell(r['title'])} | {_cell(r['agency'])} | "
                     f"{r['closes']} | {r['proposed']} |  |")
    lines += [
        "",
        f"## Scoring labels (`labels_proposed.json`): {len(scoring)} rows, {n_dis} where the"
        " model disagrees (listed first, marked ⚠)",
        "",
        HEADER,
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(scoring, start=1):
        flag = "⚠ " if r["disagrees"] else ""
        lines.append(
            f"| {i} | {flag}{r['id']} | {_cell(r['title'])} | {_cell(r['agency'])} | "
            f"{r['closes']} | {r['proposed']} | {_cell(r['model'])} | {_cell(r['reason'])} |  |")
    return "\n".join(lines) + "\n"


def write_review(today: date | None = None) -> str:
    view = json.loads(MODEL_VIEW.read_text()) if MODEL_VIEW.is_file() else {}
    md = review_markdown(
        scoring_rows(proposed(), view.get("items", {}), dataset()),
        research_rows(research_cases()),
        model_recorded_at=view.get("recorded_at"),
        today=today or datetime.now(UTC).date(),
    )
    REVIEW_MD.write_text(md)
    return md


# ---- applying the review ---------------------------------------------------------

_ROW = re.compile(r"^\|\s*(R?\d+)\s*\|\s*(?:⚠\s*)?([\w:.\-]+)\s*\|(.*)\|\s*([^|]*?)\s*\|\s*$")


def parse_review(md: str) -> tuple[dict[str, str], dict[str, str]]:
    """Each row's review mark: (scoring {id: mark}, research {id: mark})."""
    scoring, research = {}, {}
    for line in md.splitlines():
        m = _ROW.match(line.strip())
        if not m:
            continue
        num, id_, _, mark = m.groups()
        (research if num.startswith("R") else scoring)[id_] = mark.strip()
    return scoring, research


def _resolve(mark: str, proposed_label: str, allowed: tuple[str, ...]) -> str:
    low = mark.strip().lower()
    if not low:
        raise ValueError("blank")
    if low in OK_MARKS or mark.strip() in OK_MARKS:
        return proposed_label
    label = low.replace("-", "_").replace(" ", "_")
    if label not in allowed:
        raise ValueError(f"{mark!r} is not ✓ or one of {', '.join(allowed)}")
    return label


def apply_review(md: str, *, today: date | None = None) -> dict[str, Any]:
    """Validate every row, write `labels_reviewed.json`, fix research labels in place.
    Returns what changed. Raises ValueError (and writes nothing) on any bad row."""
    marks, research_marks = parse_review(md)
    labels = proposed()
    cases = research_cases()
    errors, changed, research_changed = [], {}, {}
    reviewed = {}
    for id_, lab in labels.items():
        try:
            new = _resolve(marks.get(id_, ""), lab["label"], SCORING_LABELS)
        except ValueError as e:
            errors.append(f"{id_}: {e}")
            continue
        entry = {"label": new, "reason": lab["reason"]}
        if new != lab["label"]:
            entry["proposed"] = lab["label"]
            changed[id_] = (lab["label"], new)
        reviewed[id_] = entry
    for c in cases:
        cur = c["expected"]["go_no_go"]
        try:
            new = _resolve(research_marks.get(c["id"], ""), cur, RESEARCH_LABELS)
        except ValueError as e:
            errors.append(f"{c['id']}: {e}")
            continue
        if new != cur:
            research_changed[c["id"]] = (cur, new)
            c["expected"]["go_no_go"] = new
    if errors:
        raise ValueError("review incomplete: " + "; ".join(errors))
    REVIEWED.write_text(json.dumps({
        "note": "Human-reviewed (scope S2): each label confirmed or corrected by the owner;"
                " `proposed` is kept where it changed. labels_proposed.json is history.",
        "reviewed_at": (today or datetime.now(UTC).date()).isoformat(),
        "labels": reviewed,
    }, indent=2, ensure_ascii=False) + "\n")
    if research_changed:
        RESEARCH_CASES.write_text(json.dumps(cases, indent=1))
    return {"scoring_changed": changed, "research_changed": research_changed}
