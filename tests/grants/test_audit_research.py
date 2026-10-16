"""The research audit (scope S3), offline: claims against mocked USAspending/SAM."""

import json
from datetime import date

import httpx
from agents_core.http import Http

from agents.grants.fetch_sam import SamBudget
from agents.grants.state import SamState
from scripts.audit_research import (
    BAD,
    NA,
    OK,
    Sources,
    audit_brief,
    money_figures,
    money_matches,
)
from tests.grants.conftest import make_opportunity

TODAY = date(2026, 10, 16)
ATT = "https://sam.gov/api/prod/opps/v3/opportunities/resources/files/aaa111/download"
AWARD = {"Award ID": "36C10B20F0250", "Recipient Name": "CACI NSS, LLC",
         "Award Amount": 244612345.5, "Awarding Agency": "Department of Veterans Affairs",
         "Awarding Sub Agency": "Department of Veterans Affairs",
         "generated_internal_id": "CONT_AWD_36C10B20F0250"}


def world(no_award_rows=()):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        url = request.url
        if url.host == "api.usaspending.gov" and url.path.endswith("spending_by_award/"):
            f = json.loads(request.content)["filters"]
            if "award_ids" in f:
                hits = [AWARD] if f["award_ids"] == [AWARD["Award ID"]] else []
                return httpx.Response(200, json={"results": hits})
            return httpx.Response(200, json={"results": list(no_award_rows)})
        if url.host == "api.usaspending.gov" and "/awards/" in url.path:
            return httpx.Response(200, json={
                "awarding_agency": {"toptier_agency": {"name": "Department of Veterans Affairs"},
                                    "subtier_agency": {"name": "Veterans Health Administration"}},
                "latest_transaction_contract_data": {"naics": "541512"}})
        if url.host == "sam.gov" and url.path.endswith("/resources"):
            return httpx.Response(200, json={"_embedded": {"opportunityAttachmentList": [
                {"attachments": [{"resourceId": "aaa111", "name": "SOW.pdf"}]}]}})
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


def sources(tmp_path, transport, *, budget=5):
    http = Http(cache_dir=tmp_path / "http", transport=transport, sleep=lambda s: None)
    sam = SamBudget(http, SamState(), daily_budget=budget, today=TODAY) if budget else None
    att = tmp_path / "attachments"
    att.mkdir()
    (att / "aaa111.bin.txt.json").write_text(json.dumps(
        {"type": "pdf", "text": "Estimated value $50 Million to $100 Million."}))
    return Sources(http, sam, TODAY, att), sam


def opp(**kw):
    base = dict(
        id="sam:va1", source_id="va1", agency="VETERANS AFFAIRS, DEPARTMENT OF",
        sub_agency="VETERANS AFFAIRS, DEPARTMENT OF", naics=["541512"],
        url="https://sam.gov/opp/va1/view", attachments=[ATT],
        solicitation_number="36C25527AP0383", description_text="An interface.")
    return make_opportunity(**{**base, **kw})


def block(**kw):
    base = {
        "what_theyre_buying": "An HL7 interface.",
        "evaluation_criteria": ["Not found"],
        "likely_incumbent": "CACI NSS, LLC",
        "incumbent_notes": "Similar work went to CACI NSS (36C10B20F0250, $244.6M).",
        "prior_awards": [{"award_id": "36C10B20F0250", "amount": 244612345.5,
                          "url": "https://www.usaspending.gov/award/CONT_AWD_36C10B20F0250"}],
        "risks": ["The future contract is $50M-$100M, over the $5M ceiling."],
        "rationale": "Solicitation 36C25527AP0383 is a product buy.",
        "citations": [
            {"source": "SAM.gov", "url": "https://sam.gov/opp/va1/view", "note": "The notice"},
            {"source": "SAM.gov", "url": ATT, "note": "attachment_1 (pdf)"},
            {"source": "USAspending.gov",
             "url": "https://www.usaspending.gov/award/CONT_AWD_36C10B20F0250",
             "note": "Prior award 36C10B20F0250"},
        ],
    }
    return {**base, **kw}


def statuses(audit):
    return {r.claim: r.status for r in audit.rows}


def test_money_figures_keep_their_rounding():
    [m] = money_figures("worth $244.6M")
    assert m.value == 244.6e6 and money_matches(m, 244612345.5)
    assert not money_matches(m, 245.0e6)
    assert [f.value for f in money_figures("$50 Million to $1,200")] == [50e6, 1200.0]


def test_a_correct_brief_is_all_checked(tmp_path):
    transport, calls = world()
    src, sam = sources(tmp_path, transport)
    profile = "Value range (USD): 25000.0 to 5000000.0"
    audit = audit_brief(opp(), block(), src, "Live brief 1", profile)
    s = statuses(audit)
    assert audit.failures == 0, [r for r in audit.rows if r.status == BAD]
    assert s["award 36C10B20F0250 exists (prior_awards)"] == OK
    assert s["award 36C10B20F0250 amount $244,612,345.50"] == OK
    assert s["award 36C10B20F0250 NAICS"] == OK and s["award 36C10B20F0250 agency"] == OK
    assert s["figure $244.6M"] == OK and s["figure $50M"] == OK and s["figure $100M"] == OK
    assert s["cites attachment_1 (pdf)"] == OK and s["figure $5M"] == OK
    assert s["likely incumbent 'CACI NSS, LLC'"] == OK
    # The solicitation number in the text isn't mistaken for an award id.
    assert not any("36C25527AP0383" in c for c in s)
    assert sam.used_this_run == 1  # one SAM.gov listing


def test_mismatches_are_flagged(tmp_path):
    transport, _ = world()
    src, _ = sources(tmp_path, transport, budget=0)
    bad = block(
        incumbent_notes="Went to CACI (36C10B20F0250, $310.0M) and 47QTCA99X9999.",
        likely_incumbent="ACME LLC",
        prior_awards=[{"award_id": "36C10B20F0250", "amount": 1.0,
                       "url": "https://www.usaspending.gov/award/CONT_AWD_36C10B20F0250"}],
        citations=[*block()["citations"],
                   {"source": "SAM.gov", "url": "https://sam.gov/opp/other/view",
                    "note": "Another notice"},
                   {"source": "SAM.gov", "url": "https://example.com/x",
                    "note": "attachment_2 (docx)"}],
    )
    s = statuses(audit_brief(opp(), bad, src, "x"))
    assert s["award 36C10B20F0250 amount $1.00"] == BAD
    assert s["figure $310.0M"] == BAD
    assert s["award 47QTCA99X9999 exists (text)"] == BAD
    assert s["likely incumbent 'ACME LLC'"] == BAD
    assert s["cites Another notice"] == BAD
    assert s["cites attachment_2 (docx)"] == BAD
    assert src.sam_skipped == 1  # no SAM budget: names checked by index only


def test_no_awards_claim_is_rechecked_with_the_fixed_filter(tmp_path):
    transport, calls = world(no_award_rows=[{"Award ID": "N0001", "Recipient Name": "X"}])
    src, _ = sources(tmp_path, transport, budget=0)
    navy = opp(agency="DEPT OF DEFENSE", sub_agency="DEPT OF THE NAVY")
    none = block(prior_awards=[], likely_incumbent=None,
                 incumbent_notes="No matching prior awards were found via USAspending.",
                 citations=block()["citations"][:2])
    audit = audit_brief(navy, none, src, "x")
    assert statuses(audit)["no prior awards found"] == BAD
    assert statuses(audit)["likely incumbent: none named"] == NA
    sent = [json.loads(c.content)["filters"] for c in calls
            if c.url.path.endswith("spending_by_award/")]
    assert sent[-1]["agencies"][0]["tier"] == "subtier"
