# Scope: sam-agent, next round (calibration, labels, live research audit, alerts)

Baseline (2026-10-16): last run ok, no warnings. Top 10 matches: 5 Pursue (fit
75–82), all with bid-research briefs; the rest Consider (71–73). Latest research
eval: `required_tools` 0.90, everything else 1.00, judge 0.95. Scoring stability
0.967 (2026-09-27).

Do S1–S4 in order. Log every judgment call as one line in DECISIONS.md. Stop where
the scope says OWNER. Keep all tests, ruff and actionlint green. Total Anthropic
spend cap for the round: $3.00.

## S1. Calibrate the LLM graders against human scores

The summary and research judges (`LLMJudge` in `evals/grants/suites.py`) score
0.85–0.95, but no one has checked that their scores match a person's.

1. `scripts/make_calibration_sheet.py` → `evals/grants/calibration/sheet.csv` (plus a
   simple HTML view): 20 items (10 summaries; 10 research briefs: the 5 live ones plus
   5 fixtures), each with the rubric, the output and an empty 1–5 score column. No
   judge scores shown.
2. OWNER: score the 20 items (~30 minutes); commit or upload the filled CSV.
3. Run `LLMJudge.calibrate()` on the filled sheet; record in
   `evals/grants/calibration/report.md`: agreement (Spearman's ρ, MAE), the threshold
   mapping, and where the judge disagrees most.
4. If ρ < 0.6, revise the rubric (bump `prompt_version`) and re-run once. Report
   before and after.
5. Store the calibration in `evals/history.jsonl` (`calibrated: true`, `rho`); the PR
   gate then uses calibrated pass thresholds.

Done when a report exists with ρ and MAE and the judges use calibrated thresholds. If
ρ stays low after one revision, the report says so plainly.

## S2. Review the provisional eval labels

`evals/grants/labels_proposed.json` holds 40 labels the agent proposed itself. One
known open call is `r3-nsf-grant` ("go" vs the model's "no_go").

1. `scripts/review_labels.py` writes `evals/grants/labels_review.md`: one row per label
   (title, agency, closing date, proposed label, one-line reason), rows the current
   model disagrees with flagged at the top.
2. OWNER: mark each row ✓ or a corrected label (~20–30 minutes); `r3-nsf-grant` first
   with both arguments.
3. Write `labels_reviewed.json` (the evals read it; keep the proposed file), re-run
   the scoring and research suites once (≤ $1.00), record the delta.

Done when every label is human-reviewed, the evals read the reviewed file, and the new
baseline is in `history.jsonl`.

## S3. Audit the live bid-research briefs

1. `scripts/audit_research.py` re-fetches, from each brief's citations, every award ID
   and dollar figure (USAspending) and every attachment name (SAM), and checks: cited
   awards exist and amounts match; agency and NAICS match; attachment names match; the
   likely incumbent appears in the cited awards; nothing cites an unfetched source.
2. `docs/research_audit_2026-10.md`: a table per brief (claim → source → ✓/✗) plus SAM
   request usage (within the ~10/day key budget).
3. Fix any mismatch at its cause with a regression test from the real response
   (trimmed fixture).
4. Fix `required_tools` 0.90: real miss or fixture/expectation issue; resolve it.

Done when every claim in all 5 briefs is ✓ (or fixed and re-verified), `required_tools`
is 1.00 or explained, and SAM usage is within budget.

## S4. Email alerts for strong matches (owner only)

1. `[alerts]` in `config/grants.toml`: `enabled`, `min_fit = 80`,
   `recommendations = ["Pursue"]`, `max_per_day = 5`; `to` from a secret.
2. After a successful run, select new opportunities (or ones newly crossing the
   threshold) not alerted before; track by `notice_id` + content hash.
3. One digest email per run: title, agency, fit, recommendation, closing date with days
   left, value, set-aside, the 2-line fit reason, links to the site and SAM.gov /
   Grants.gov.
4. Delivery: Resend HTTP API (or SMTP) from the workflow. OWNER: create the key, add
   secrets `ALERT_EMAIL_API_KEY` and `ALERT_EMAIL_TO` (verify a sending domain or use
   the provider's test sender). Missing secrets: skip with a `meta.warnings` line, never
   fail the run.
5. Plain text plus simple HTML, no tracking pixels, an unsubscribe note (`enabled=false`).
6. Tests: selection (threshold, dedupe, max per day, rescored item crossing the
   threshold), rendering snapshot, no-secret path, a dry-run flag that writes the email
   to the job summary.

Done when a real digest arrives from a manual run and a second run sends nothing new.

## Out of scope

Changing the business profile; auto-applying or submitting bids; emailing anyone but
the owner; models other than the existing agents-core tiers.
