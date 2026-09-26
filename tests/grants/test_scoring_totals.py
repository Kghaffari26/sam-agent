from datetime import UTC, date, datetime

import pytest
from agents_core.costs import CostTracker
from agents_core.llm import LLM

from agents.grants.config import GrantsConfig
from agents.grants.scoring import (
    HARD_CAP,
    SCORE_PROMPT_VERSION,
    RubricOutput,
    cache_key_matches,
    clamp_sub_scores,
    custom_id_for,
    finalize,
    hard_incompatibility,
    recommendation_for,
    score_opportunities,
    scrub,
)
from tests.grants.conftest import make_opportunity, make_profile
from tests.grants.fakes import FakeClient, default_rubric

NOW = datetime(2026, 9, 24, tzinfo=UTC)
TODAY = date(2026, 9, 24)
CONFIG = GrantsConfig()


def rubric(**overrides) -> RubricOutput:
    base = dict(capability=30, eligibility=15, size=8, timeline=10, strategic=5,
                reasons=["Good match"], red_flags=[], confidence="high")
    base.update(overrides)
    return RubricOutput(**base)


def fin(raw, opp=None, profile=None):
    return finalize(raw, opp or make_opportunity(description_fetched=True),
                    profile or make_profile(), CONFIG, profile_hash="p", model="m", now=NOW)


def test_sub_scores_are_clamped_to_their_ranges():
    subs = clamp_sub_scores(rubric(capability=99, eligibility=-4, size=16, timeline=15,
                                   strategic=11))
    assert subs.model_dump() == {"capability": 40, "eligibility": 0, "size": 15,
                                 "timeline": 15, "strategic": 10}


def test_fit_is_the_sum_of_clamped_sub_scores():
    score = fin(rubric(capability=45))
    assert score.fit == 40 + 15 + 8 + 10 + 5
    assert score.fit == sum(score.sub_scores.model_dump().values())


@pytest.mark.parametrize(("fit", "rec"), [(75, "Pursue"), (74, "Consider"), (55, "Consider"),
                                          (54, "Pass"), (0, "Pass")])
def test_recommendation_bands(fit, rec):
    assert recommendation_for(fit, CONFIG) == rec


@pytest.mark.parametrize("flag", ["Requires Secret clearance", "TS/SCI required",
                                  "Active security clearance needed", "8(a) only"])
def test_hard_incompatibility_caps_fit_at_20(flag):
    score = fin(rubric(capability=40, eligibility=20, size=15, timeline=15, strategic=10,
                       red_flags=[flag]))
    assert score.fit == HARD_CAP and score.capped and score.recommendation == "Pass"


def test_clearance_the_profile_holds_is_not_a_blocker():
    profile = make_profile(clearance="secret")
    assert hard_incompatibility(["Requires Secret clearance"], profile) is None
    assert hard_incompatibility(["Requires TS/SCI"], profile) is not None
    assert hard_incompatibility(["8(a) only"], make_profile(certifications=["8A"])) is None


def test_soft_red_flags_do_not_cap():
    score = fin(rubric(red_flags=["Incumbent strongly implied"]))
    assert not score.capped and score.fit == 68


def test_no_description_and_low_confidence_caps_at_70():
    raw = rubric(capability=40, eligibility=20, size=15, timeline=15, strategic=10,
                 confidence="low")
    capped = fin(raw, opp=make_opportunity(description_fetched=False))
    assert capped.fit == 70 and capped.capped
    assert fin(raw, opp=make_opportunity(description_fetched=True)).fit == 100
    medium = fin(raw.model_copy(update={"confidence": "medium"}),
                 opp=make_opportunity(description_fetched=False))
    assert medium.fit == 100


def test_reasons_trimmed_to_three_of_twelve_words():
    score = fin(rubric(reasons=["one two three four five six seven eight nine ten eleven "
                                "twelve thirteen", "b", "c", "d"]))
    assert len(score.reasons) == 3
    assert len(score.reasons[0].split()) == 12


def test_cache_key_covers_content_profile_prompt_and_model():
    opp = make_opportunity(content_hash="c1")
    score = fin(rubric(), opp=opp)
    assert score.prompt_version == SCORE_PROMPT_VERSION
    assert cache_key_matches(score, opp, profile_hash="p", model="m")
    assert not cache_key_matches(score, opp.model_copy(update={"content_hash": "c2"}),
                                 profile_hash="p", model="m")
    assert not cache_key_matches(score, opp, profile_hash="other", model="m")
    assert not cache_key_matches(score, opp, profile_hash="p", model="other")
    assert not cache_key_matches(score.model_copy(update={"prompt_version": "v0"}), opp,
                                 profile_hash="p", model="m")
    assert not cache_key_matches(None, opp, profile_hash="p", model="m")


def test_custom_ids_are_batch_safe():
    assert custom_id_for("sam:5b345bbb") == "sam_5b345bbb"
    assert custom_id_for("gg:" + "9" * 100) == ("gg_" + "9" * 100)[:64]


def test_scrub_drops_unsupported_numbers_and_backfills_reasons():
    opp = make_opportunity()
    raw = rubric(reasons=["Worth $9.9M to you"], red_flags=["On-site 4 days a week",
                                                           "Incumbent implied"])
    out = scrub(raw, facts=[4.0], opp=opp, profile=make_profile())
    assert out.red_flags == ["On-site 4 days a week", "Incumbent implied"]
    assert out.reasons == ["Primary NAICS match", "IT/professional services PSC"]
    assert out.capability == raw.capability


# ---- running it against the fake client ---------------------------------------------


def make_llm(client, tmp_path, max_usd=0.50):
    tracker = CostTracker(agent="grants", run_id="t", max_usd=max_usd,
                          path=tmp_path / "costs.jsonl")
    return LLM(tracker, client=client, sleep=lambda s: None), tracker


def test_scores_via_batch_and_logs_costs(tmp_path):
    client = FakeClient()
    llm, tracker = make_llm(client, tmp_path)
    opps = [make_opportunity(id=f"sam:{i}", source_id=str(i)) for i in range(3)]
    run = score_opportunities(llm, opps, make_profile(), CONFIG, profile_hash="p",
                              today=TODAY, now=NOW)
    assert run.mode == "batch" and run.scored == 3 and not run.failed
    assert len(client.batch_requests) == 1 and not client.sync_calls
    assert tracker.calls == 3 and (tmp_path / "costs.jsonl").is_file()
    request = client.batch_requests[0][0]
    assert request["custom_id"] == "sam_0"
    assert "<<<UNTRUSTED>>>" not in request["params"]["messages"][0]["content"]  # no desc


def test_guard_failure_is_retried_then_scrubbed(tmp_path):
    def invents_numbers(prompt):
        out = default_rubric(prompt)
        out["reasons"] = ["Award worth $7.3M", "Python modernization scope"]
        return out

    client = FakeClient(rubric=invents_numbers)
    llm, _ = make_llm(client, tmp_path)
    run = score_opportunities(llm, [make_opportunity()], make_profile(), CONFIG,
                              profile_hash="p", today=TODAY, now=NOW)
    score = run.scores["sam:abc123"]
    assert score.reasons == ["Python modernization scope"]
    assert len(client.sync_calls) == 1  # the one guard retry


def test_batch_timeout_falls_back_to_sync(tmp_path):
    client = FakeClient(batch_polls=10**6)
    llm, _ = make_llm(client, tmp_path)
    config = GrantsConfig.model_validate({"settings": {"batch_poll_timeout_min": 1}})
    run = score_opportunities(llm, [make_opportunity()], make_profile(), config,
                              profile_hash="p", today=TODAY, now=NOW, poll_seconds=30)
    assert client.cancelled == ["batch_1"]
    assert run.mode == "sync" and run.scored == 1


def test_over_budget_batch_is_shrunk_not_overspent(tmp_path):
    client = FakeClient()
    llm, tracker = make_llm(client, tmp_path, max_usd=0.004)
    opps = [make_opportunity(id=f"sam:{i}", source_id=str(i)) for i in range(8)]
    run = score_opportunities(llm, opps, make_profile(), CONFIG, profile_hash="p",
                              today=TODAY, now=NOW)
    assert 0 < run.scored < 8
    assert set(run.failed) | set(run.scores) == {o.id for o in opps}
    assert not client.sync_calls  # deferred items wait for the next run
    assert tracker.total_usd <= 0.004


@pytest.mark.parametrize("flag", ["DoD contract; no clearance held",
                                  "DoD/DISA requires security clearance likely",
                                  "May require a clearance", "Clearance status unclear"])
def test_speculative_clearance_flags_do_not_cap(flag):
    assert hard_incompatibility([flag], make_profile()) is None


def test_refinalize_reapplies_caps_and_bands_without_llm():
    from agents.grants.scoring import refinalize

    opp = make_opportunity(description_fetched=True)
    stale = fin(rubric()).model_copy(update={"fit": 20, "recommendation": "Pass",
                                             "capped": True, "prompt_version": "v2"})
    fresh = refinalize(stale, opp, make_profile(), CONFIG)
    assert fresh.fit == 68 and fresh.recommendation == "Consider" and not fresh.capped
    assert fresh.prompt_version == "v2" and fresh.content_hash == stale.content_hash
