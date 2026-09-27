"""Bid research (§6.3): attachments, USAspending, and the agent loop's tools,
guard, caps and fallbacks, against the fixture world with a scripted model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agents_core.costs import CostTracker
from agents_core.llm import LLM

from agents.grants.attachments import (
    UnsupportedAttachment,
    attachment_names,
    extract_text,
    resource_id,
)
from agents.grants.research import cache_valid, candidates, to_cache
from agents.grants.usaspending import agency_name, award_query, parse_awards
from evals.grants import research_world
from tests.grants.fakes import BRIEF, FakeClient

WORLD = research_world.WORLD
CASES = {c.id: c for c in research_world.cases()}


def use(id_: str, tool_name: str, /, **input_) -> dict:
    return {"type": "tool_use", "id": id_, "name": tool_name, "input": input_}


def scripted(*turns):
    """A research responder that plays `turns` in order (each a list of blocks)."""
    queue = list(turns)

    def respond(params):
        return queue.pop(0)

    return respond


def llm_for(client: FakeClient, tmp_path: Path, max_usd: float = 1.0) -> LLM:
    return LLM(CostTracker(agent="t", run_id="t", max_usd=max_usd,
                           path=tmp_path / "costs.jsonl"), client=client)


def run(tmp_path, case_id, *turns, settings=None, max_usd=1.0, world=None):
    client = FakeClient(research=scripted(*turns))
    outcome = research_world.run_research(
        llm_for(client, tmp_path, max_usd), CASES[case_id], tmp_path, settings=settings,
        world=world,
    )
    return outcome, client


def brief(**overrides) -> dict:
    return {**BRIEF, **overrides}


def tool_results(client: FakeClient, n: int) -> list[dict]:
    """The tool_result blocks sent back to the model in request `n` (0-based)."""
    return client.research_calls[n]["messages"][-1]["content"]


# ---- attachments -----------------------------------------------------------------


def test_pdf_and_docx_text_extraction():
    kind, text = extract_text((WORLD / "files" / "r1sow0000000000000000000000000001.pdf")
                              .read_bytes())
    assert kind == "pdf" and "SECTION M - EVALUATION FACTORS FOR AWARD" in text
    kind, text = extract_text((WORLD / "files" / "r1prc0000000000000000000000000002.docx")
                              .read_bytes())
    assert kind == "docx" and "Table 1: Technical Lead" in text


@pytest.mark.parametrize("data", [b"<html>not a file</html>", b"PK\x03\x04garbage", b""])
def test_other_files_are_unsupported(data):
    with pytest.raises(UnsupportedAttachment):
        extract_text(data)


def test_attachment_names_follow_resource_links(opportunity_factory):
    url = "https://sam.gov/api/prod/opps/v3/opportunities/resources/files/abc123/download"
    opp = opportunity_factory(attachments=[url, url.replace("abc123", "def456")])
    assert attachment_names(opp) == [("attachment_1", url),
                                     ("attachment_2", url.replace("abc123", "def456"))]
    assert resource_id(url) == "abc123"


# ---- USAspending -----------------------------------------------------------------


@pytest.mark.parametrize("sam,usa", [
    ("VETERANS AFFAIRS, DEPARTMENT OF", "Department of Veterans Affairs"),
    ("DEPT OF DEFENSE", "Department of Defense"),
    ("GENERAL SERVICES ADMINISTRATION", "General Services Administration"),
    ("National Science Foundation", "National Science Foundation"),
    ("TREASURY, DEPARTMENT OF THE", "Department of the Treasury"),
])
def test_agency_names_map_to_usaspending_toptier(sam, usa):
    assert agency_name(sam) == usa


def test_award_query_for_contracts_and_grants():
    from datetime import date

    q = award_query(agency="DEPT OF DEFENSE", naics="541512", keywords=["cloud", "x"],
                    kind="contract", today=date(2026, 9, 27))
    f = q["filters"]
    assert f["award_type_codes"] == ["A", "B", "C", "D"]
    assert f["naics_codes"] == {"require": ["541512"]} and f["keywords"] == ["cloud"]
    assert f["agencies"][0]["name"] == "Department of Defense"
    assert f["time_period"][0] == {"start_date": "2021-09-28", "end_date": "2026-09-27"}
    g = award_query(agency=None, naics="541512", keywords=[], kind="grant",
                    today=date(2026, 9, 27))["filters"]
    assert g["award_type_codes"] == ["02", "03", "04", "05"]
    assert "naics_codes" not in g and "agencies" not in g and "keywords" not in g


def test_parse_awards_copies_values_and_links_usaspending():
    data = json.loads((WORLD / "usaspending.json").read_text())["veterans"]
    awards = parse_awards(data)
    assert awards[0]["award_id"] == "36C10B21C0045" and awards[0]["amount"] == 3412500.0
    assert awards[0]["url"] == ("https://www.usaspending.gov/award/"
                                "CONT_AWD_36C10B21C0045_3600_-NONE-_-NONE-")
    with pytest.raises(ValueError):
        parse_awards({"error": "bad request"})


# ---- the loop --------------------------------------------------------------------

R1 = "sam:r1va0000000000000000000000000001"
R5 = "sam:r5ts0000000000000000000000000005"
GG = "gg:990001"
VA = "VETERANS AFFAIRS, DEPARTMENT OF"


def test_full_research_publishes_code_built_prior_awards_and_citations(tmp_path):
    good = brief(
        likely_incumbent="ACME DIGITAL SERVICES LLC",
        incumbent_notes="Award 36C10B21C0045 to ACME DIGITAL SERVICES LLC was $3,412,500"
                        " through 2026-04-30.",
        prior_award_ids=["36C10B21C0045"],
    )
    outcome, client = run(
        tmp_path, "r1-va-dashboard",
        [use("a", "get_opportunity", id=R1), use("b", "list_attachments", id=R1)],
        [use("c", "read_attachment", id=R1, name="attachment_1"),
         use("d", "read_attachment", id=R1, name="attachment_2"),
         use("e", "usaspending_prior_awards", agency=VA, naics="541512",
             keywords=["dashboard"])],
        [use("f", "finish", **good)],
    )
    block = outcome.block
    assert outcome.loop.stop_reason == "finished" and outcome.cacheable
    assert block.status == "complete" and block.narrative_source == "llm"
    assert block.steps == 3 and block.tools_used == [
        "get_opportunity", "list_attachments", "read_attachment", "usaspending_prior_awards"]
    [award] = block.prior_awards  # amounts come from USAspending, never the model
    assert (award.award_id, award.amount, award.recipient) == (
        "36C10B21C0045", 3412500.0, "ACME DIGITAL SERVICES LLC")
    sources = [c.source for c in block.citations]
    assert sources == ["SAM.gov", "SAM.gov", "SAM.gov", "USAspending.gov"]
    assert outcome.session.env.sam_requests_used == 2  # two downloads, both in the ledger
    assert outcome.session.env.sam_budget.state.requests_today(research_world.AS_OF) == 2
    # Every tool output reached the model wrapped as untrusted data.
    for block_ in tool_results(client, 1):
        assert block_["content"].startswith("<untrusted-tool-output")


def test_invented_numbers_are_retried_then_templated(tmp_path):
    invented = brief(rationale="A $7,300,000 ceiling makes this worth it.")
    outcome, client = run(
        tmp_path, "r2-gsa-sources-sought",
        [use("a", "get_opportunity", id="sam:r2gsa000000000000000000000000002")],
        [use("b", "finish", **invented)],
        [use("c", "finish", **invented)],
    )
    assert outcome.loop.guard_attempts == 2
    retry = tool_results(client, 2)[0]
    assert retry["is_error"] and "7,300,000" in retry["content"]
    block = outcome.block
    assert block.narrative_source == "template" and "template" in block.rationale
    assert "7,300,000" not in block.rationale


def test_unknown_award_ids_fail_the_guard_then_pass_after_retry(tmp_path):
    outcome, _ = run(
        tmp_path, "r2-gsa-sources-sought",
        [use("a", "usaspending_prior_awards", agency="GENERAL SERVICES ADMINISTRATION")],
        [use("b", "finish", **brief(prior_award_ids=["47QTCA99X9999"]))],
        [use("c", "finish", **brief(prior_award_ids=["47QTCA22F0033"]))],
    )
    assert outcome.loop.ok and outcome.loop.guard_attempts == 2
    assert outcome.block.narrative_source == "llm"
    assert [a.award_id for a in outcome.block.prior_awards] == ["47QTCA22F0033"]


def test_attachment_reads_are_capped_at_three_per_opportunity(tmp_path):
    reads = [use(f"r{i}", "read_attachment", id=R5, name=f"attachment_{i}")
             for i in range(1, 5)]
    outcome, client = run(tmp_path, "r5-disa-top-secret", reads,
                          [use("f", "finish", **brief(go_no_go="no_go"))])
    results = tool_results(client, 1)
    assert [bool(r.get("is_error")) for r in results] == [False, False, False, True]
    assert "limit reached" in results[3]["content"]
    assert outcome.session.env.sam_requests_used == 3
    assert len(outcome.block.citations) == 1 + 3


def test_research_sam_requests_are_capped_per_run(tmp_path):
    settings = research_world.research_settings().model_copy(
        update={"max_sam_requests_per_run": 1})
    outcome, client = run(
        tmp_path, "r1-va-dashboard",
        [use("a", "read_attachment", id=R1, name="attachment_1"),
         use("b", "read_attachment", id=R1, name="attachment_2")],
        [use("f", "finish", **brief())],
        settings=settings,
    )
    first, second = tool_results(client, 1)
    assert not first.get("is_error") and "request limit" in second["content"]
    assert outcome.session.env.sam_requests_used == 1


def test_wrong_source_tools_and_other_ids_are_refused(tmp_path):
    outcome, client = run(
        tmp_path, "r1-va-dashboard",
        [use("a", "grants_gov_detail", id=R1), use("b", "get_opportunity", id="sam:other")],
        [use("f", "finish", **brief())],
    )
    wrong, other = tool_results(client, 1)
    assert wrong["is_error"] and "Not a Grants.gov opportunity" in wrong["content"]
    assert other["is_error"] and R1 in other["content"]
    assert outcome.loop.tools_called() == ["grants_gov_detail", "get_opportunity"]


def test_grants_gov_detail_for_a_grant(tmp_path):
    outcome, client = run(
        tmp_path, "r3-nsf-grant",
        [use("a", "grants_gov_detail", id=GG), use("b", "list_attachments", id=GG)],
        [use("f", "finish", **brief())],
    )
    detail, attachments = tool_results(client, 1)
    assert "Intellectual Merit" in detail["content"] and '"award_ceiling": "1500000"' in \
        detail["content"]
    assert attachments["is_error"]
    assert [c.source for c in outcome.block.citations] == ["Grants.gov", "Grants.gov"]


def test_a_budget_stop_publishes_a_partial_template(tmp_path):
    settings = research_world.research_settings().model_copy(update={"max_steps": 1})
    outcome, _ = run(tmp_path, "r1-va-dashboard",
                     [use("a", "get_opportunity", id=R1)], settings=settings)
    assert outcome.loop.stop_reason == "max_steps" and outcome.cacheable
    assert outcome.block.status == "partial" and outcome.block.narrative_source == "template"
    assert outcome.block.go_no_go == "go"  # the rubric said Pursue


def test_run_budget_stop_is_not_cached(tmp_path):
    outcome, client = run(tmp_path, "r1-va-dashboard", max_usd=0.0001)
    assert outcome.loop.stop_reason == "run_budget" and not outcome.cacheable
    assert client.research_calls == []


def test_no_sam_key_means_no_downloads_but_cached_text_is_free(tmp_path):
    world = research_world.World()
    outcome, client = run(
        tmp_path, "r1-va-dashboard",
        [use("a", "read_attachment", id=R1, name="attachment_1")],
        [use("f", "finish", **brief())], world=world,
    )
    downloads = [c for c in world.calls if c.url.host == "sam.gov"]
    assert len(downloads) == 1
    # Second loop, same attachments dir: served from the extracted text, no request.
    again, _ = run(
        tmp_path, "r1-va-dashboard",
        [use("a", "read_attachment", id=R1, name="attachment_1")],
        [use("f", "finish", **brief())], world=world,
    )
    assert len([c for c in world.calls if c.url.host == "sam.gov"]) == 1
    assert again.session.env.sam_requests_used == 0


def test_cache_key_and_candidates(opportunity_factory, tmp_path):
    outcome, _ = run(tmp_path, "r2-gsa-sources-sought",
                     [use("f", "finish", **brief())])
    opp, score = research_world.case_objects(CASES["r2-gsa-sources-sought"])
    cache = to_cache(outcome.block, opp, profile_hash="p")
    assert cache_valid(cache, opp, profile_hash="p", model=outcome.block.model)
    changed = opp.model_copy(update={"content_hash": "new"})
    assert not cache_valid(cache, changed, profile_hash="p", model=outcome.block.model)
    assert not cache_valid(cache, opp, profile_hash="q", model=outcome.block.model)
    consider = score.model_copy(update={"recommendation": "Consider"})
    scores = {"a": score, "b": consider, "c": score, "d": score, "e": score}
    assert candidates(["a", "b", "c", "d", "e"], scores, {"c"}, 2) == ["a", "d"]
