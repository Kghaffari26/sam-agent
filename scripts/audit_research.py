"""Audit the published bid research briefs against their own sources (scope S3).

    uv run python -m scripts.audit_research [--sam-request-budget 5] [--rerun-research]

For every brief in `data/grants/store.json.gz` it re-fetches what the brief cites and
checks each claim:

- every award id in `prior_awards` or in the text exists on USAspending.gov (a fresh
  `spending_by_award` search by award id), its amount matches the brief (exactly for
  `prior_awards`, to the brief's rounding for a "$244.6M" in the text), and its
  awarding agency and NAICS (award detail endpoint) match the notice's;
- every other dollar figure in the text appears in the notice or a cited attachment;
- `likely_incumbent` is the recipient of a cited award;
- every cited attachment is the notice's `resourceLinks` entry at that index, its
  type matches the file, and (with SAM budget) its real file name from SAM.gov;
- every citation points at a source the research fetched (the notice, an attachment
  it read, a USAspending award it cites);
- a "no prior awards found" claim is re-checked with the agency filter fixed (a
  sub-agency as a USAspending subtier); finding awards makes it ✗.

Attachment text comes from the agent's cache (`.cache/grants/attachments/`, restored
in CI from the daily run's cache) and costs nothing. SAM.gov file listings (public
sam.gov, not the API key) go through `fetch_sam.SamBudget` against the committed
ledger (`data/grants/state.json`), capped by `--sam-request-budget` and by what the
day's budget has left; the ledger isn't saved (CI doesn't commit it from a branch).

`--rerun-research` then re-researches the same opportunities with the current code
(real LLM calls, capped by `--max-usd`; attachments from the cache) and audits those
briefs too, without touching the store. Writes `--out` (markdown) and `--json`.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from agents_core.http import HostPolicy, Http, HttpError, RequestBudgetExceeded

from agents.grants.attachments import attachment_cache_dir, attachment_names, resource_id
from agents.grants.config import load_business_profile, load_grants_config
from agents.grants.fetch_sam import SamBudget
from agents.grants.models import Opportunity, Score
from agents.grants.state import load_state
from agents.grants.usaspending import (
    AWARD_SEARCH_URL,
    CONTRACT_TYPES,
    GRANT_TYPES,
    USA_HOST,
    agency_name,
    award_query,
    awarding_agency_filter,
)

REPO = Path(__file__).resolve().parents[1]
STORE = REPO / "data" / "grants" / "store.json.gz"
STATE = REPO / "data" / "grants" / "state.json"
OUT = REPO / "docs" / "research_audit_2026-10.md"
AWARD_DETAIL_URL = "https://api.usaspending.gov/api/v2/awards/{}/"
SAM_RESOURCES_URL = "https://sam.gov/api/prod/opps/v3/opportunities/{}/resources"
OK, BAD, NA = "✓", "✗", "–"

# Award ids look like 36C10B20F0250 / 47QTCA22F0033 / HT001125C0012: letters and
# digits, at least 10 characters, at least 4 digits.
_AWARD_ID = re.compile(r"\b(?=[A-Z0-9-]*\d[A-Z0-9-]*\d[A-Z0-9-]*\d[A-Z0-9-]*\d)"
                       r"(?=[A-Z0-9-]*[A-Z])[A-Z0-9][A-Z0-9-]{8,}[A-Z0-9]\b")
_MONEY = re.compile(
    r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(k|m|b|thousand|million|billion|mm)?\b", re.I)
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6, "b": 1e9,
          "billion": 1e9}
_NO_AWARDS = re.compile(r"\bno (?:matching |relevant |similar )?(?:prior )?(?:\w+ )?awards?\b"
                        r"|\bno incumbent could be identified", re.I)


@dataclass
class Money:
    text: str
    value: float
    tolerance: float  # half a unit of the figure's last digit


def money_figures(text: str) -> list[Money]:
    out = []
    for m in _MONEY.finditer(text):
        digits, unit = m.group(1).replace(",", ""), (m.group(2) or "").lower()
        scale = _SCALE.get(unit, 1.0)
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        out.append(Money(m.group(0).strip(), float(digits) * scale,
                         0.5 * scale / 10**decimals if scale > 1 or decimals else 0.5))
    return out


def money_matches(figure: Money, value: float | None) -> bool:
    return value is not None and abs(figure.value - value) <= figure.tolerance + 0.005


def text_has_number(figure: Money, text: str) -> bool:
    """A bare number in `text` (the profile writes "5000000.0") within the figure's
    rounding."""
    return any(money_matches(figure, float(n.replace(",", "")))
               for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text))


def text_has_money(figure: Money, text: str) -> bool:
    return any(money_matches(figure, other.value) or money_matches(other, figure.value)
               for other in money_figures(text))


@dataclass
class Row:
    claim: str
    source: str
    status: str
    detail: str = ""


@dataclass
class BriefAudit:
    opportunity_id: str
    title: str
    label: str
    rows: list[Row] = field(default_factory=list)

    def add(self, claim: str, source: str, ok: bool | None, detail: str = "") -> None:
        self.rows.append(Row(claim, source, NA if ok is None else OK if ok else BAD, detail))

    @property
    def failures(self) -> int:
        return sum(r.status == BAD for r in self.rows)


def narrative(block: dict[str, Any]) -> str:
    return "\n".join([block["what_theyre_buying"], *block["evaluation_criteria"],
                      block.get("likely_incumbent") or "", block["incumbent_notes"],
                      *block["risks"], block["rationale"]])


# ---- sources ------------------------------------------------------------------------


class Sources:
    """Everything the audit fetches, each fetched once."""

    def __init__(self, http: Http, sam: SamBudget | None, today: date,
                 attachments_dir: Path) -> None:
        self.http, self.sam, self.today = http, sam, today
        self.attachments_dir = attachments_dir
        self._awards: dict[str, dict[str, Any] | None] = {}
        self._details: dict[str, dict[str, Any] | None] = {}
        self._listings: dict[str, list[dict[str, Any]] | None] = {}
        self.sam_skipped = 0

    def award(self, award_id: str, kind: str) -> dict[str, Any] | None:
        """The award as the agent's search returns it, found by its id."""
        if award_id not in self._awards:
            body = {
                "filters": {"award_ids": [award_id],
                            "award_type_codes": GRANT_TYPES if kind == "grant"
                            else CONTRACT_TYPES},
                "fields": award_query(agency=None, naics=None, keywords=[], kind=kind,
                                      today=self.today)["fields"],
                "limit": 10, "page": 1,
            }
            rows = self.http.request("POST", AWARD_SEARCH_URL, json_body=body).json()
            hit = next((r for r in rows.get("results") or []
                        if str(r.get("Award ID")) == award_id), None)
            self._awards[award_id] = hit
        return self._awards[award_id]

    def detail(self, internal_id: str) -> dict[str, Any] | None:
        if internal_id not in self._details:
            try:
                self._details[internal_id] = self.http.request(
                    "GET", AWARD_DETAIL_URL.format(internal_id)).json()
            except HttpError:
                self._details[internal_id] = None
        return self._details[internal_id]

    def search(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        return self.http.request("POST", AWARD_SEARCH_URL, json_body=query).json().get(
            "results") or []

    def attachment_text(self, url: str) -> dict[str, Any] | None:
        txt = self.attachments_dir / f"{resource_id(url)}.bin.txt.json"
        try:
            return json.loads(txt.read_text())
        except (OSError, ValueError):
            return None

    def listing(self, notice_id: str) -> list[dict[str, Any]] | None:
        """SAM.gov's public attachment listing for a notice (one budgeted request)."""
        if notice_id in self._listings:
            return self._listings[notice_id]
        result = None
        if self.sam is not None:
            dest = self.attachments_dir.parent / "audit" / f"{notice_id}.resources.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.sam.download(SAM_RESOURCES_URL.format(notice_id), dest)
                data = json.loads(dest.read_bytes())
                result = [a for att in (data.get("_embedded") or {}).get(
                    "opportunityAttachmentList") or [] for a in att.get("attachments") or []]
            except (RequestBudgetExceeded, HttpError, ValueError, OSError):
                self.sam_skipped += 1
        else:
            self.sam_skipped += 1
        self._listings[notice_id] = result
        return result


def _detail_naics(detail: dict[str, Any] | None) -> str | None:
    if not detail:
        return None
    contract = detail.get("latest_transaction_contract_data") or {}
    return contract.get("naics") or (detail.get("naics_hierarchy") or {}).get(
        "base_code", {}).get("code")


def _detail_agencies(detail: dict[str, Any] | None) -> tuple[str | None, str | None]:
    agency = (detail or {}).get("awarding_agency") or {}
    return ((agency.get("toptier_agency") or {}).get("name"),
            (agency.get("subtier_agency") or {}).get("name"))


# ---- the audit ------------------------------------------------------------------------


def audit_brief(opp: Opportunity, block: dict[str, Any], src: Sources, label: str,
                profile_text: str = "") -> BriefAudit:
    a = BriefAudit(opp.id, opp.title, label)
    text = narrative(block)
    attachments = dict(attachment_names(opp))
    read_texts: list[str] = []

    # Citations and attachments.
    award_urls = {str(p["url"]) for p in block["prior_awards"]}
    for c in block["citations"]:
        url, note = c["url"], c.get("note") or ""
        if url == str(opp.url):
            a.add(f"cites the notice ({c['source']})", url, True)
            continue
        m = re.match(r"(attachment_\d+) \((\w+)\)", note)
        if m:
            name, kind = m.groups()
            index_ok = attachments.get(name) == url
            cached = src.attachment_text(url)
            type_ok = None if cached is None else cached.get("type") == kind
            if cached:
                read_texts.append(cached.get("text") or "")
            listing = src.listing(opp.source_id)
            real = None
            if listing is not None:
                real = next((f for f in listing if f.get("resourceId") == resource_id(url)),
                            None)
            detail = [f"index {'matches' if index_ok else 'does NOT match'} resourceLinks"]
            if cached is None:
                detail.append("file not in the attachment cache (type unchecked)")
            else:
                detail.append(f"file type {cached.get('type')}")
            if real is not None:
                detail.append(f"SAM.gov name {real.get('name')!r}")
            elif listing is not None:
                detail.append("NOT in SAM.gov's current attachment list")
            ok = index_ok and type_ok is not False and (listing is None or real is not None)
            a.add(f"cites {name} ({kind})", url, ok, "; ".join(detail))
            continue
        if c["source"] == "USAspending.gov":
            a.add(f"cites {note}", url, url in award_urls,
                  "" if url in award_urls else "not a prior award in the brief")
            continue
        a.add(f"cites {note or c['source']}", url, False, "not a source the research fetched")

    # Award ids: prior_awards plus any in the text.
    excluded = {opp.solicitation_number or "", opp.source_id}
    text_ids = [i for i in dict.fromkeys(_AWARD_ID.findall(text))
                if i not in excluded and not any(i in e or e in i for e in excluded if e)]
    ids = list(dict.fromkeys([p["award_id"] for p in block["prior_awards"]] + text_ids))
    found: dict[str, dict[str, Any]] = {}
    top = agency_name(opp.agency) if opp.agency else None
    sub = agency_name(opp.sub_agency) if opp.sub_agency else None
    for award_id in ids:
        hit = src.award(award_id, opp.kind)
        where = "prior_awards" if any(p["award_id"] == award_id
                                      for p in block["prior_awards"]) else "text"
        if hit is None:
            a.add(f"award {award_id} exists ({where})", "USAspending search by award id",
                  False, "not found")
            continue
        found[award_id] = hit
        amount = hit.get("Award Amount")
        a.add(f"award {award_id} exists ({where})", "USAspending search by award id", True,
              f"{hit.get('Recipient Name')}, ${float(amount or 0):,.2f}")
        published = next((p for p in block["prior_awards"] if p["award_id"] == award_id), None)
        if published is not None:
            a.add(f"award {award_id} amount ${published['amount'] or 0:,.2f}", "USAspending",
                  amount is not None and abs(float(amount) - (published["amount"] or 0)) < 0.01,
                  f"USAspending ${float(amount or 0):,.2f}")
        detail = src.detail(hit["generated_internal_id"]) if hit.get(
            "generated_internal_id") else None
        a_top, a_sub = _detail_agencies(detail)
        a_top = a_top or hit.get("Awarding Agency")
        a_sub = a_sub or hit.get("Awarding Sub Agency")
        agency_ok = (top or "").lower() == (a_top or "").lower()
        a.add(f"award {award_id} agency", "USAspending award", agency_ok,
              f"{a_top} / {a_sub} vs notice {top} / {sub}")
        naics = _detail_naics(detail)
        if opp.kind == "grant":
            a.add(f"award {award_id} NAICS", "USAspending award", None, "grant: no NAICS")
        else:
            a.add(f"award {award_id} NAICS", "USAspending award",
                  None if naics is None else naics in opp.naics,
                  f"{naics or 'not reported'} vs notice {', '.join(opp.naics) or '-'}")

    # Dollar figures in the text.
    source_text = "\n".join([opp.description_text or "", opp.title, *read_texts])
    seen: set[str] = set()
    for sentence in re.split(r"(?<=[.;])\s+|\n", text):
        for fig in money_figures(sentence):
            if fig.text in seen:
                continue
            seen.add(fig.text)
            near = [i for i in found if i in sentence]
            if near and any(money_matches(fig, float(found[i].get("Award Amount") or 0))
                            for i in near):
                a.add(f"figure {fig.text}", f"USAspending award {', '.join(near)}", True)
            elif text_has_money(fig, source_text):
                a.add(f"figure {fig.text}", "notice / cited attachment text", True)
            elif text_has_number(fig, profile_text):
                a.add(f"figure {fig.text}", "business profile", True)
            else:
                a.add(f"figure {fig.text}", "notice, attachments, cited awards", False,
                      f"not found (in: {sentence.strip()[:120]})")

    # Likely incumbent.
    incumbent = block.get("likely_incumbent")
    if incumbent:
        names = {str(found[i].get("Recipient Name") or "").lower() for i in found}
        a.add(f"likely incumbent {incumbent!r}", "cited awards' recipients",
              incumbent.lower() in names, ", ".join(sorted(names)) or "no cited awards")
    else:
        a.add("likely incumbent: none named", "-", None)

    # "No prior awards" re-checked with the fixed agency filter.
    if not block["prior_awards"] and _NO_AWARDS.search(block["incumbent_notes"]):
        flt = awarding_agency_filter(opp.sub_agency or opp.agency, agency=opp.agency,
                                     sub_agency=opp.sub_agency)
        query = award_query(agency=None, naics=opp.naics[0] if opp.naics else None,
                            keywords=[], kind=opp.kind, today=src.today, agency_filter=flt)
        rows = src.search(query)
        detail = (f"{flt['tier']} {flt['name']!r}, NAICS {opp.naics[0] if opp.naics else '-'}"
                  f": {len(rows)} awards")
        if rows:
            r0 = rows[0]
            detail += f" (largest {r0.get('Award ID')} {r0.get('Recipient Name')})"
        a.add("no prior awards found", "USAspending, agency filter fixed", not rows, detail)
    return a


# ---- running and reporting --------------------------------------------------------


def load_briefs(path: Path = STORE) -> list[tuple[Opportunity, Score, dict[str, Any]]]:
    items = json.loads(gzip.decompress(path.read_bytes()))["items"]
    out = []
    for e in items.values():
        if e.get("research"):
            out.append((Opportunity.model_validate(e["opportunity"]),
                        Score.model_validate(e["score"]), e["research"]["block"]))
    out.sort(key=lambda t: (-t[1].fit, t[0].id))
    return out


def rerun_research(briefs, http: Http, sam: SamBudget | None, today: date, max_usd: float):
    from agents_core.costs import CostTracker
    from agents_core.llm import LLM

    from agents.grants.research import ResearchEnv, research_one

    profile = load_business_profile(REPO / "config" / "business_profile.toml")
    settings = load_grants_config(REPO / "config" / "grants.toml").research
    now = datetime.now(UTC)
    tracker = CostTracker(agent="audit:grants-research", run_id=now.strftime(
        "%Y-%m-%dT%H-%M-%SZ"), max_usd=max_usd)
    llm = LLM(tracker)
    env = ResearchEnv(http=http, profile=profile, today=today, settings=settings,
                      sam_budget=sam, attachments_dir=attachment_cache_dir())
    out = []
    for opp, score, _ in briefs:
        outcome = research_one(llm, env, opp, score, now=now)
        out.append((opp, score, outcome.block.model_dump(mode="json")))
    return out, tracker.total_usd


def _cell(v: str) -> str:
    return v.replace("|", "\\|").replace("\n", " ")


def report(audits: list[BriefAudit], *, sam_used: int, sam_budget: int, sam_day_total: int,
           daily_budget: int, sam_skipped: int, usd: float, today: date) -> str:
    lines = [
        "# Bid research audit, 2026-10",
        "",
        f"_Generated {today.isoformat()} by `scripts/audit_research.py` on GitHub Actions"
        " (USAspending.gov and sam.gov aren't reachable from the dev container)._",
        "",
        f"SAM.gov requests: {sam_used} this audit (cap {sam_budget}); the ledger for"
        f" {today.isoformat()} stands at {sam_day_total} of the daily {daily_budget}."
        + (f" {sam_skipped} attachment listings skipped (no budget)." if sam_skipped else "")
        + f" LLM spend: ${usd:.4f}.",
        "",
    ]
    for audit in audits:
        n_bad = audit.failures
        lines += [f"## {audit.label}: {audit.title}", "",
                  f"`{audit.opportunity_id}` — {len(audit.rows)} checks, "
                  f"{'all ✓' if not n_bad else f'{n_bad} ✗'}", "",
                  "| claim | source | match | detail |", "|---|---|---|---|"]
        for r in audit.rows:
            lines.append(f"| {_cell(r.claim)} | {_cell(r.source)} | {r.status} | "
                         f"{_cell(r.detail)} |")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", type=Path, default=STORE)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--json", type=Path, default=OUT.with_suffix(".json"))
    ap.add_argument("--sam-request-budget", type=int, default=5)
    ap.add_argument("--rerun-research", action="store_true")
    ap.add_argument("--max-usd", type=float, default=0.40)
    args = ap.parse_args(argv)

    today = datetime.now(UTC).date()
    config = load_grants_config(REPO / "config" / "grants.toml")
    state = load_state(STATE)
    http = Http(cache_dir=REPO / ".cache" / "http")
    http.set_policy(USA_HOST, HostPolicy(min_interval_seconds=0.5))
    left = max(config.settings.sam_daily_request_budget - state.sam.requests_today(today), 0)
    cap = min(args.sam_request_budget, left)
    sam = SamBudget(http, state.sam, daily_budget=state.sam.requests_today(today) + cap,
                    today=today) if cap > 0 else None
    src = Sources(http, sam, today, attachment_cache_dir())

    from agents.grants.scoring import profile_context

    profile_text = profile_context(
        load_business_profile(REPO / "config" / "business_profile.toml"))
    briefs = load_briefs(args.store)
    audits = [audit_brief(opp, block, src, f"Live brief {i}", profile_text)
              for i, (opp, _, block) in enumerate(briefs, start=1)]
    usd = 0.0
    rerun_blocks = []
    if args.rerun_research:
        rerun, usd = rerun_research(briefs, http, sam, today, args.max_usd)
        rerun_blocks = [{"opportunity_id": o.id, "block": b} for o, _, b in rerun]
        audits += [audit_brief(opp, block, src, f"Re-researched {i}", profile_text)
                   for i, (opp, _, block) in enumerate(rerun, start=1)]
    md = report(audits, sam_used=sam.used_this_run if sam else 0, sam_budget=cap,
                sam_day_total=state.sam.requests_today(today),
                daily_budget=config.settings.sam_daily_request_budget, sam_skipped=src.sam_skipped,
                usd=usd, today=today)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(md)
    args.json.write_text(json.dumps({
        "audits": [{"opportunity_id": x.opportunity_id, "label": x.label,
                    "rows": [r.__dict__ for r in x.rows]} for x in audits],
        "rerun_blocks": rerun_blocks,
    }, indent=1, ensure_ascii=False) + "\n")
    print(md)
    http.close()
    return 1 if any(x.failures for x in audits) else 0


if __name__ == "__main__":
    sys.exit(main())
