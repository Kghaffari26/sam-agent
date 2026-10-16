"""Calibrating the LLM judges (`judge_quality` in the summaries and research suites)
against human scores.

    uv run python -m scripts.make_calibration_sheet     # freeze 20 items, write the sheet
    # a human fills in score_1_to_5 in evals/grants/calibration/sheet.csv
    uv run python -m scripts.calibrate_judges           # judge them, write report.md

Items are frozen in `calibration/items.json` (live data changes daily): 10 published
summaries (the store's top matches by fit with an LLM-written summary), the 5 live bid
research briefs, and 5 fixture briefs replayed offline from the last research eval's
trajectories. The sheet shows the rubric, the opportunity and the output, never a
judge score, so the human isn't anchored.

`calibrate` runs `LLMJudge.calibrate` (agents-core) per rubric and adds what it
doesn't report: Spearman's rho and a threshold mapping (the judge pass threshold that
best reproduces the human pass/fail, a human pass being 4 or 5 of 5). The result goes
to `calibration/calibration.json` (read by `suites.py` for the judges' pass
thresholds), `calibration/report.md`, and one `grants-judge-calibration` line in
`evals/history.jsonl`.
"""

from __future__ import annotations

import csv
import gzip
import html
import json
import math
import random
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents_core.evals import CalibrationReport, EvalCase, LabeledExample, LLMJudge, git_sha
from agents_core.llm import LLM

HERE = Path(__file__).resolve().parent
CAL_DIR = HERE / "calibration"
ITEMS_PATH = CAL_DIR / "items.json"
SHEET_CSV = CAL_DIR / "sheet.csv"
SHEET_HTML = CAL_DIR / "sheet.html"
CALIBRATION_JSON = CAL_DIR / "calibration.json"
REPORT_MD = CAL_DIR / "report.md"
REPO_ROOT = HERE.parents[1]
STORE_PATH = REPO_ROOT / "data" / "grants" / "store.json.gz"
TRAJECTORIES = HERE / "trajectories"

N_SUMMARIES = 10
HUMAN_PASS = 4  # of 5: a human "pass" is 4 or 5
DEFAULT_PASS_THRESHOLD = 0.75  # LLMJudge's default, (4 - 1) / 4
CANDIDATE_THRESHOLDS = (0.25, 0.5, 0.75, 1.0)  # judge scores are (1..5 - 1) / 4
MIN_RHO = 0.6
SCORE_COLUMN = "score_1_to_5"
CSV_COLUMNS = ["item_id", "kind", "source", "title", "rubric", "opportunity", "output",
               SCORE_COLUMN, "notes"]
SHUFFLE_SEED = 20261016
OPP_HIDDEN = {"first_seen_at", "last_seen_at", "content_hash", "attachments"}


# ---- building the items ----------------------------------------------------------


def _opp_input(opp: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in opp.items() if k not in {"first_seen_at", "last_seen_at"}}


def summary_items(store: dict[str, Any], n: int = N_SUMMARIES) -> list[dict[str, Any]]:
    """The top `n` stored items by fit whose summary the LLM wrote."""
    rows = [e for e in store.values()
            if e.get("summary") and e["summary"].get("narrative_source") == "llm"
            and e.get("score")]
    rows.sort(key=lambda e: (-e["score"]["fit"], e["opportunity"]["id"]))
    out = []
    for e in rows[:n]:
        s = e["summary"]
        out.append({
            "kind": "summary",
            "source": "live",
            "opportunity_id": e["opportunity"]["id"],
            "title": e["opportunity"]["title"],
            "input": _opp_input(e["opportunity"]),
            "output": {k: s[k] for k in ("what_they_want", "why_fit", "risks", "next_steps")},
        })
    return out


def live_research_items(store: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [e for e in store.values() if e.get("research")]
    rows.sort(key=lambda e: (-e["score"]["fit"], e["opportunity"]["id"]))
    return [{
        "kind": "research",
        "source": "live",
        "opportunity_id": e["opportunity"]["id"],
        "title": e["opportunity"]["title"],
        "input": {"opportunity": e["opportunity"], "score": e["score"]},
        "output": e["research"]["block"],
    } for e in rows]


def fixture_research_items() -> list[dict[str, Any]]:
    """The five research eval cases, replayed offline from their saved trajectories."""
    from evals.grants import research_world
    from evals.grants.demo import replay

    out = []
    for case in research_world.cases():
        with tempfile.TemporaryDirectory() as tmp:
            outcome = replay(case.id, Path(tmp), TRAJECTORIES / f"{case.id}.json")
        out.append({
            "kind": "research",
            "source": "fixture",
            "opportunity_id": case.id,
            "title": case.input["opportunity"]["title"],
            "input": case.input,
            "output": outcome.block.model_dump(mode="json"),
        })
    return out


def load_store(path: Path = STORE_PATH) -> dict[str, Any]:
    return json.loads(gzip.decompress(path.read_bytes()))["items"]


def build_items(store: dict[str, Any]) -> list[dict[str, Any]]:
    """Summaries first, then research; shuffled within each kind (so live and fixture
    briefs aren't scored in a predictable order) and numbered."""
    rng = random.Random(SHUFFLE_SEED)
    summaries = summary_items(store)
    research = live_research_items(store) + fixture_research_items()
    rng.shuffle(summaries)
    rng.shuffle(research)
    items = []
    for i, item in enumerate(summaries + research, start=1):
        items.append({"item_id": f"c{i:02d}", **item})
    return items


# ---- the sheet -------------------------------------------------------------------


def rubrics() -> dict[str, str]:
    from evals.grants.suites import RESEARCH_RUBRIC, SUMMARY_RUBRIC

    return {"summary": SUMMARY_RUBRIC, "research": RESEARCH_RUBRIC}


def _bullets(values: Sequence[str]) -> str:
    return "\n".join(f"- {v}" for v in values) if values else "- (none)"


def opportunity_text(item: dict[str, Any], *, description_chars: int = 1200) -> str:
    opp = item["input"]["opportunity"] if item["kind"] == "research" else item["input"]
    value = opp.get("value_amount")
    lines = [
        f"{opp.get('title')}",
        f"Agency: {opp.get('agency')} / {opp.get('sub_agency') or '-'} / "
        f"{opp.get('office') or '-'}",
        f"Type: {opp.get('notice_type')}  NAICS: {', '.join(opp.get('naics') or []) or '-'}"
        f"  Set-aside: {opp.get('set_aside_label') or '-'}",
        f"Deadline: {opp.get('deadline') or '-'}  Value: "
        f"{'-' if value is None else f'${value:,.0f}'}",
        f"Link: {opp.get('url')}",
    ]
    desc = (opp.get("description_text") or "").strip()
    if desc:
        more = "..." if len(desc) > description_chars else ""
        lines.append(f"Description: {desc[:description_chars]}{more}")
    if item["kind"] == "research":
        score = item["input"]["score"]
        lines.append(f"Rubric fit: {score['fit']} ({score['recommendation']})")
    return "\n".join(lines)


def output_text(item: dict[str, Any]) -> str:
    o = item["output"]
    if item["kind"] == "summary":
        return "\n".join([
            f"What they want: {o['what_they_want']}",
            "Why it fits:", _bullets(o["why_fit"]),
            "Risks:", _bullets(o["risks"]),
            "Next steps:", _bullets(o["next_steps"]),
        ])
    awards = [f"{a['award_id']}: {a.get('recipient') or '?'}, "
              f"{'?' if a.get('amount') is None else f'${a['amount']:,.2f}'}"
              for a in o.get("prior_awards") or []]
    return "\n".join([
        f"What they're buying: {o['what_theyre_buying']}",
        "Evaluation criteria:", _bullets(o["evaluation_criteria"]),
        f"Likely incumbent: {o.get('likely_incumbent') or '(none)'}",
        f"Incumbent notes: {o['incumbent_notes']}",
        "Prior awards (copied from USAspending in code):", _bullets(awards),
        "Risks:", _bullets(o["risks"]),
        f"Go/no-go: {o['go_no_go']}",
        f"Rationale: {o['rationale']}",
    ])


def sheet_rows(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    rub = rubrics()
    return [{
        "item_id": it["item_id"],
        "kind": it["kind"],
        "source": it["source"],
        "title": it["title"],
        "rubric": rub[it["kind"]],
        "opportunity": opportunity_text(it),
        "output": output_text(it),
        SCORE_COLUMN: "",
        "notes": "",
    } for it in items]


def write_csv(rows: list[dict[str, str]], path: Path = SHEET_CSV) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)


HTML_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Judge calibration sheet</title>
<style>
:root { --bg:#fff; --fg:#1b1b1b; --muted:#5f6368; --card:#f6f7f9; --line:#d9dce1;
        --accent:#1a5fb4; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16181c; --fg:#e8eaed; --muted:#9aa0a6; --card:#20242a; --line:#343a42;
          --accent:#8ab4f8; } }
body { margin:0; padding:16px; background:var(--bg); color:var(--fg);
       font:15px/1.5 system-ui, sans-serif; }
main { max-width:860px; margin:0 auto; }
.item { background:var(--card); border:1px solid var(--line); border-radius:8px;
        padding:12px 16px; margin:16px 0; }
h2 { font-size:1.05rem; margin:0 0 4px; }
.meta { color:var(--muted); font-size:.85rem; }
pre { white-space:pre-wrap; word-wrap:break-word; font:inherit; margin:6px 0; }
details { margin:6px 0; }
summary { cursor:pointer; color:var(--accent); }
fieldset { border:0; padding:0; margin:8px 0 0; display:flex; gap:12px; flex-wrap:wrap; }
textarea { width:100%; box-sizing:border-box; margin-top:6px; background:var(--bg);
           color:var(--fg); border:1px solid var(--line); border-radius:4px; }
button { font:inherit; padding:8px 14px; border-radius:6px; border:1px solid var(--accent);
         background:var(--accent); color:var(--bg); cursor:pointer; }
.bar { position:sticky; bottom:0; background:var(--bg); padding:10px 0;
       border-top:1px solid var(--line); display:flex; gap:12px; align-items:center; }
</style></head><body><main>
<h1>Judge calibration sheet</h1>
<p>Score each output 1-5 against its rubric (5 = fully meets it). Judge scores are
deliberately not shown. When done, press <b>Download CSV</b> and commit the file as
<code>evals/grants/calibration/sheet.csv</code> (or send it back).</p>
"""

HTML_TAIL = """<div class="bar"><button id="dl">Download CSV</button>
<span id="count" class="meta"></span></div>
</main>
<script>
const rows = JSON.parse(document.getElementById('rows').textContent);
function count() {
  const n = rows.filter(r => document.querySelector(`input[name="${r.item_id}"]:checked`)).length;
  document.getElementById('count').textContent = `${n} of ${rows.length} scored`;
}
document.addEventListener('change', count); count();
function q(v) { return '"' + String(v).replaceAll('"', '""') + '"'; }
document.getElementById('dl').onclick = () => {
  const cols = COLUMNS;
  const lines = [cols.join(',')];
  for (const r of rows) {
    const s = document.querySelector(`input[name="${r.item_id}"]:checked`);
    const out = {...r, score_1_to_5: s ? s.value : '',
                 notes: document.getElementById('n-' + r.item_id).value};
    lines.push(cols.map(c => q(out[c] ?? '')).join(','));
  }
  const blob = new Blob([lines.join('\\r\\n') + '\\r\\n'], {type: 'text/csv'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'sheet.csv'; a.click();
};
</script></body></html>
"""


def write_html(rows: list[dict[str, str]], path: Path = SHEET_HTML) -> None:
    parts = [HTML_HEAD]
    seen_rubric: set[str] = set()
    for r in rows:
        e = html.escape
        rubric = (f"<details{' open' if r['kind'] not in seen_rubric else ''}>"
                  f"<summary>Rubric ({e(r['kind'])})</summary><pre>{e(r['rubric'])}</pre>"
                  "</details>")
        seen_rubric.add(r["kind"])
        radios = "".join(
            f'<label><input type="radio" name="{e(r["item_id"])}" value="{v}"> {v}</label>'
            for v in range(1, 6))
        parts.append(
            f'<section class="item"><h2>{e(r["item_id"])} · {e(r["title"])}</h2>'
            f'<div class="meta">{e(r["kind"])} · {e(r["source"])}</div>{rubric}'
            f"<details><summary>Opportunity</summary><pre>{e(r['opportunity'])}</pre>"
            f"</details><pre>{e(r['output'])}</pre>"
            f"<fieldset><legend>Score</legend>{radios}</fieldset>"
            f'<textarea id="n-{e(r["item_id"])}" rows="2" placeholder="Notes (optional)">'
            "</textarea></section>")
    data = json.dumps([{k: v for k, v in r.items() if k not in (SCORE_COLUMN, "notes")}
                       for r in rows]).replace("</", "<\\/")
    parts.append(f'<script type="application/json" id="rows">{data}</script>')
    parts.append(HTML_TAIL.replace("COLUMNS", json.dumps(CSV_COLUMNS)))
    path.write_text("".join(parts), encoding="utf-8")


def make_sheet(store_path: Path = STORE_PATH) -> list[dict[str, Any]]:
    items = build_items(load_store(store_path))
    CAL_DIR.mkdir(parents=True, exist_ok=True)
    ITEMS_PATH.write_text(json.dumps(items, indent=1, ensure_ascii=False) + "\n")
    rows = sheet_rows(items)
    write_csv(rows)
    write_html(rows)
    return items


# ---- reading the filled sheet and calibrating ---------------------------------------


def read_scores(path: Path = SHEET_CSV) -> dict[str, int]:
    """Human scores by item id; raises if any row is blank or not 1-5."""
    scores, bad = {}, []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            raw = (row.get(SCORE_COLUMN) or "").strip()
            try:
                value = int(float(raw))
            except ValueError:
                value = 0
            if not 1 <= value <= 5:
                bad.append(f"{row.get('item_id')}: {raw!r}")
            else:
                scores[row["item_id"]] = value
    if bad:
        raise ValueError(f"{len(bad)} rows without a 1-5 score: {', '.join(bad)}")
    return scores


def _ranks(xs: Sequence[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 2:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / (sx * sy)


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Spearman's rho (Pearson on average ranks); None when either side is constant."""
    return _pearson(_ranks(xs), _ranks(ys))


def threshold_mapping(pairs: Sequence[tuple[float, float]]) -> dict[str, Any]:
    """For each judge threshold, how often judge pass == human pass (human >= 4/5);
    the best one (ties: closest to the default 0.75), and the mean human score at
    each judge score."""
    human_pass = (HUMAN_PASS - 1) / 4
    agreement = {
        t: sum((j >= t) == (h >= human_pass) for j, h in pairs) / len(pairs)
        for t in CANDIDATE_THRESHOLDS
    }
    best = max(CANDIDATE_THRESHOLDS,
               key=lambda t: (agreement[t], -abs(t - DEFAULT_PASS_THRESHOLD)))
    by_level: dict[int, list[float]] = {}
    for j, h in pairs:
        by_level.setdefault(round(j * 4) + 1, []).append(h * 4 + 1)
    return {
        "agreement_by_threshold": {f"{t:.2f}": round(a, 3) for t, a in agreement.items()},
        "pass_threshold": best,
        "human_mean_by_judge_score": {
            str(k): round(sum(v) / len(v), 2) for k, v in sorted(by_level.items())},
    }


def examples(items: list[dict[str, Any]], scores: dict[str, int],
             kind: str) -> list[tuple[dict[str, Any], LabeledExample]]:
    out = []
    for it in items:
        if it["kind"] != kind:
            continue
        case = EvalCase(id=it["item_id"], input=it["input"])
        out.append((it, LabeledExample(case=case, output=it["output"],
                                       human_score=(scores[it["item_id"]] - 1) / 4)))
    return out


def summarize_report(report: CalibrationReport, rows: list[tuple[dict[str, Any], float, float]]
                     ) -> dict[str, Any]:
    js = [j for _, j, _ in rows]
    hs = [h for _, _, h in rows]
    rho = spearman(js, hs)
    mapping = threshold_mapping(report.pairs)
    worst = sorted(rows, key=lambda r: -abs(r[1] - r[2]))[:5]
    return {
        "n": report.n,
        "rho": None if rho is None else round(rho, 3),
        "mae": round(report.mean_abs_error, 3),
        "mae_points": round(report.mean_abs_error * 4, 2),  # on the 1-5 scale
        "bias": round(report.bias, 3),
        "pearson": None if report.correlation is None else round(report.correlation, 3),
        "agreement_at_0_75": round(report.agreement, 3),
        **mapping,
        "calibrated": rho is not None and rho >= MIN_RHO,
        "largest_disagreements": [
            {"item_id": it["item_id"], "title": it["title"], "source": it["source"],
             "judge": round(j * 4 + 1), "human": round(h * 4 + 1)}
            for it, j, h in worst if abs(j - h) > 0
        ],
    }


def calibrate_kind(llm: LLM, judge: LLMJudge, items: list[dict[str, Any]],
                   scores: dict[str, int], kind: str) -> dict[str, Any]:
    exs = examples(items, scores, kind)
    report = judge.calibrate(llm, [ex for _, ex in exs])
    rows = [(it, j, h) for (it, _), (j, h) in zip(exs, report.pairs, strict=True)]
    return summarize_report(report, rows)


def history_line(result: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    kinds = {k: v for k, v in result.items() if k in ("summaries", "research")}
    return {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "suite": "grants-judge-calibration",
        "prompt_version": result["rubric_version"],
        "git_sha": git_sha(),
        "model": result["model"],
        "calibrated": all(v["calibrated"] for v in kinds.values()),
        "rho": {k: v["rho"] for k, v in kinds.items()},
        "pass_thresholds": {k: v["pass_threshold"] for k, v in kinds.items()},
        "scores": {f"{k}_{m}": v[m] for k, v in kinds.items() for m in ("rho", "mae")
                   if v[m] is not None},
        "usd": result["usd"],
        "n_cases": sum(v["n"] for v in kinds.values()),
        "n_scored": sum(v["n"] for v in kinds.values()),
    }


def pass_threshold(kind: str, path: Path = CALIBRATION_JSON) -> float:
    """The calibrated pass threshold for a judge ("summaries" or "research"), or the
    default when the judge isn't calibrated (no file, or rho below `MIN_RHO`)."""
    try:
        data = json.loads(path.read_text())[kind]
    except (OSError, KeyError, ValueError):
        return DEFAULT_PASS_THRESHOLD
    return float(data["pass_threshold"]) if data.get("calibrated") else DEFAULT_PASS_THRESHOLD


def _fmt(v: Any) -> str:
    return "—" if v is None else f"{v:.3f}" if isinstance(v, float) else str(v)


def report_markdown(runs: list[dict[str, Any]]) -> str:
    lines = [
        "# Judge calibration",
        "",
        "LLM judges (`judge_quality`) vs. the owner's 1-5 scores on 20 frozen items "
        "(`items.json`, `sheet.csv`). Generated by `scripts/calibrate_judges.py`.",
        "A judge counts as calibrated at Spearman's ρ ≥ 0.6; its pass threshold is then "
        "the one that best reproduces the human pass/fail (human pass = 4 or 5).",
        "",
    ]
    for run in runs:
        lines += [f"## Rubric {run['rubric_version']} ({run['ts']}, ${run['usd']:.4f})", ""]
        lines += ["| Judge | n | Spearman ρ | MAE (0-1) | MAE (points) | bias | Pearson "
                  "| pass threshold | calibrated |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for kind in ("summaries", "research"):
            v = run[kind]
            lines.append(
                f"| {kind} | {v['n']} | {_fmt(v['rho'])} | {_fmt(v['mae'])} | "
                f"{v['mae_points']} | {_fmt(v['bias'])} | {_fmt(v['pearson'])} | "
                f"{v['pass_threshold']} | {'yes' if v['calibrated'] else 'no'} |")
        for kind in ("summaries", "research"):
            v = run[kind]
            lines += ["", f"### {kind}: threshold mapping", "",
                      "Agreement with the human pass/fail at each judge threshold: "
                      + ", ".join(f"{t} → {a}" for t, a in v["agreement_by_threshold"].items()),
                      "", "Mean human score at each judge score: "
                      + ", ".join(f"judge {k} → human {m}"
                                  for k, m in v["human_mean_by_judge_score"].items()),
                      "", "Largest disagreements:", ""]
            if not v["largest_disagreements"]:
                lines.append("- none")
            for d in v["largest_disagreements"]:
                lines.append(f"- {d['item_id']} ({d['source']}) {d['title'][:80]}: "
                             f"judge {d['judge']}, human {d['human']}")
        lines.append("")
    return "\n".join(lines)
