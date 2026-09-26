"""The grants agent, registered with agents-core as `grants`
(`[project.entry-points."agents_core.agents"]` in pyproject.toml).

    uv run agents-run grants [--dry-run] [--rescore-all] [--lookback-days=N]
                             [--sam-request-budget=N]

fetch     SAM.gov window (budgeted) + Grants.gov search2/fetchOpportunity
transform normalize -> dedupe -> merge into store -> hard filters -> relevance
          (--dry-run stops here and prints the reject table + top 20; no LLM)
analyze   rubric scoring (Batch API, cached) -> top 20 -> budgeted SAM
          description fetches (+ rescore on change) -> summaries (cached,
          guarded) -> latest.json / all.json bodies

Local run state lives under `agents_core.settings.data_dir()/grants/`
(`data/grants/` by default, committed back by run-agent.yml): `state.json`
(SAM window + request ledger, profile hash, prompt versions), `store.json.gz`
(active opportunities with their cached scores/summaries) and
`grants_gov_details.json.gz` (the permanent fetchOpportunity cache).
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from agents_core import settings
from agents_core.agent import Agent, AgentResult, RunContext
from agents_core.http import HostPolicy, Http, HttpError, RequestBudgetExceeded
from agents_core.schema import Source
from pydantic import ValidationError

from agents.grants import scoring, summarize
from agents.grants.config import (
    BusinessProfile,
    GrantsConfig,
    load_business_profile,
    load_grants_config,
    profile_hash,
)
from agents.grants.fetch_grants_gov import (
    GG_HOST,
    MIN_INTERVAL_SECONDS,
    DetailCache,
    GrantsGovSchemaError,
    fetch_details,
    search_all,
)
from agents.grants.fetch_sam import (
    SAM_HOST,
    SamBudget,
    SamFetchResult,
    fetch_description,
    fetch_window,
    sam_window,
)
from agents.grants.filters import REJECT_REASONS, check_hard_filters, partition
from agents.grants.models import Opportunity, Score, Summary
from agents.grants.normalize import (
    normalize_grants_gov,
    normalize_sam,
    sam_description_url,
    with_sam_description,
)
from agents.grants.output import RunFacts, build_documents, rank_top
from agents.grants.relevance import compute_relevance
from agents.grants.schema import FetchedCounts, GrantsLatest
from agents.grants.state import GrantsState, load_state, profile_changed, save_state
from agents.grants.store import (
    StoreEntry,
    dedupe,
    dedupe_within_source,
    load_store,
    merge_into_store,
    prune_store,
    save_store,
)

log = logging.getLogger(__name__)

CONFIG_PATH = Path("config/grants.toml")


def grants_data_dir() -> Path:
    return settings.data_dir() / "grants"


def state_path() -> Path:
    return grants_data_dir() / "state.json"


def store_path() -> Path:
    return grants_data_dir() / "store.json.gz"


def details_path() -> Path:
    return grants_data_dir() / "grants_gov_details.json.gz"


@dataclass
class Options:
    rescore_all: bool = False
    lookback_days: int | None = None
    sam_request_budget: int | None = None


def parse_options(extra_args: list[str]) -> Options:
    parser = argparse.ArgumentParser(prog="agents-run grants", add_help=False)
    parser.add_argument("--rescore-all", action="store_true")
    parser.add_argument("--lookback-days", type=int)
    parser.add_argument("--sam-request-budget", type=int)
    args, unknown = parser.parse_known_args(extra_args)
    if unknown:
        log.warning("ignoring unknown arguments: %s", unknown)
    return Options(args.rescore_all, args.lookback_days, args.sam_request_budget)


@dataclass
class RawFetch:
    now: datetime
    today: date
    options: Options
    config: GrantsConfig
    profile: BusinessProfile
    state: GrantsState
    store: dict[str, StoreEntry]
    sam: SamFetchResult | None
    sam_budget: SamBudget | None
    sam_api_key: str | None
    gg_hits: list[dict[str, Any]]
    gg_details: DetailCache
    gg_error: str | None
    gg_detail_fetches: int


@dataclass
class Prepared:
    raw: RawFetch
    active: dict[str, Opportunity]
    relevance: dict[str, int]
    rejected: dict[str, int]
    new_ids: set[str]
    changed_ids: set[str]
    above: list[str]  # ids at/above the relevance threshold, best first
    below_count: int
    fetched: FetchedCounts
    skipped_records: int = 0
    notes: list[str] = field(default_factory=list)


class GrantsAgent(Agent):
    id = "grants"
    name = "Grants & Contracts Finder"
    route = "/grants"
    schema_version = "1.0.0"
    expected_interval_hours = 24
    next_run_hint = "Daily 06:00 PT"
    history_keep = 90
    output_model = GrantsLatest

    def __init__(self, config_path: Path | str = CONFIG_PATH) -> None:
        self.config_path = Path(config_path)

    def load_config(self) -> tuple[GrantsConfig, BusinessProfile]:
        config = load_grants_config(self.config_path)
        return config, load_business_profile(config.settings.profile)

    def configure_http(self, http: Http) -> None:
        config, _ = self.load_config()  # fails fast on a bad config/profile
        http.set_policy(
            SAM_HOST, HostPolicy(daily_budget=config.settings.sam_daily_request_budget)
        )
        http.set_policy(GG_HOST, HostPolicy(min_interval_seconds=MIN_INTERVAL_SECONDS))

    # ---- fetch --------------------------------------------------------------------

    def fetch(self, ctx: RunContext) -> RawFetch:
        options = parse_options(ctx.extra_args)
        config, profile = self.load_config()
        now = ctx.started_at.astimezone(UTC)
        today = now.date()
        state = load_state(state_path())
        store = load_store(store_path())

        sam, budget, api_key = self._fetch_sam(ctx, config, state, options, today)
        save_state(state, state_path())  # the SAM ledger is saved even on dry runs

        gg_hits, cache, gg_error, n_details = self._fetch_grants_gov(
            ctx, config, profile, now, today
        )
        return RawFetch(
            now=now,
            today=today,
            options=options,
            config=config,
            profile=profile,
            state=state,
            store=store,
            sam=sam,
            sam_budget=budget,
            sam_api_key=api_key,
            gg_hits=gg_hits,
            gg_details=cache,
            gg_error=gg_error,
            gg_detail_fetches=n_details,
        )

    def _fetch_sam(
        self,
        ctx: RunContext,
        config: GrantsConfig,
        state: GrantsState,
        options: Options,
        today: date,
    ) -> tuple[SamFetchResult | None, SamBudget | None, str | None]:
        api_key = os.environ.get("SAM_API_KEY")
        if not api_key:
            log.warning("SAM_API_KEY not set; skipping SAM.gov (never called without a key)")
            return None, None, None
        daily = config.settings.sam_daily_request_budget
        if options.sam_request_budget is not None:
            daily = min(daily, options.sam_request_budget)
        ctx.http.set_policy(SAM_HOST, HostPolicy(daily_budget=daily))
        budget = SamBudget(ctx.http, state.sam, daily_budget=daily, today=today)
        window = sam_window(
            state.sam,
            today=today,
            overlap_days=config.sam.window_overlap_days,
            first_run_lookback_days=config.sam.first_run_lookback_days,
            lookback_days=options.lookback_days,
        )
        log.info("SAM budget: %d of %d requests left today", budget.remaining(), daily)
        result = fetch_window(budget, api_key, window=window, ptypes=config.sam.ptypes)
        if result.auth_failed:
            state.sam.key_rejected_at = today.isoformat()
        elif result.records or result.complete:
            state.sam.key_rejected_at = None
        return result, budget, api_key

    def _fetch_grants_gov(
        self,
        ctx: RunContext,
        config: GrantsConfig,
        profile: BusinessProfile,
        now: datetime,
        today: date,
    ) -> tuple[list[dict[str, Any]], DetailCache, str | None, int]:
        cache = DetailCache.load(details_path())
        if not profile.include_grants:
            return [], cache, None, 0
        try:
            hits = search_all(
                ctx.http,
                profile.grant_keywords,
                opp_statuses=config.grants_gov.opp_statuses,
                rows=config.grants_gov.rows_per_query,
            )
        except (GrantsGovSchemaError, HttpError) as e:
            log.error("Grants.gov portion failed: %s", e)
            return [], cache, str(e), 0

        # Prefilter on the search hit alone, then fetch details for the most
        # relevant new/changed ids (cached ones are free), up to the cap.
        ranked: list[tuple[int, dict[str, Any]]] = []
        for hit in hits:
            try:
                opp = normalize_grants_gov(hit, now=now, detail=cache.get(hit))
            except (ValueError, KeyError, ValidationError):
                continue
            if check_hard_filters(opp, profile, today=today) is None:
                ranked.append((compute_relevance(opp, profile), hit))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        n = fetch_details(
            ctx.http,
            [hit for _, hit in ranked],
            cache,
            max_fetches=config.settings.max_grants_detail_fetches,
        )
        cache.prune(hits)
        cache.save(details_path())
        log.info("grants.gov: %d unique hits, %d details fetched", len(hits), n)
        return hits, cache, None, n

    # ---- transform ----------------------------------------------------------------

    def transform(self, ctx: RunContext, raw: RawFetch) -> Prepared:
        profile, config = raw.profile, raw.config
        stored = {id_: e.opportunity for id_, e in raw.store.items()}
        fetched: list[Opportunity] = []
        skipped = 0

        for record in raw.sam.records if raw.sam else []:
            prior = stored.get(f"sam:{record.get('noticeId')}")
            description = None
            if prior is not None and prior.description_fetched:
                description = prior.description_text or ""
            try:
                fetched.append(normalize_sam(record, now=raw.now, description_text=description))
            except (ValueError, KeyError, ValidationError) as e:
                skipped += 1
                log.debug("skipping SAM record %s: %s", record.get("noticeId"), e)

        for hit in raw.gg_hits:
            try:
                fetched.append(
                    normalize_grants_gov(hit, now=raw.now, detail=raw.gg_details.get(hit))
                )
            except (ValueError, KeyError, ValidationError) as e:
                skipped += 1
                log.debug("skipping Grants.gov hit %s: %s", hit.get("id"), e)

        merged, new_ids, changed_ids = merge_into_store(stored, dedupe(fetched), now=raw.now)
        pool = dedupe_within_source(list(merged.values()))  # collapse amendments store-wide
        survivors, rejected = partition(pool, profile, today=raw.today)
        relevance = {o.id: compute_relevance(o, profile) for o in survivors}
        active = prune_store(
            {o.id: o for o in survivors},
            today=raw.today,
            forecast_max_age_days=config.settings.forecast_max_age_days,
            store_max_items=config.settings.store_max_items,
            relevance_by_id=relevance,
        )
        threshold = config.settings.relevance_threshold
        above = sorted(
            (id_ for id_ in active if relevance[id_] >= threshold),
            key=lambda id_: (-relevance[id_], id_),
        )
        return Prepared(
            raw=raw,
            active=active,
            relevance={id_: relevance[id_] for id_ in active},
            rejected={r: rejected.get(r, 0) for r in REJECT_REASONS},
            new_ids=new_ids & set(active),
            changed_ids=changed_ids & set(active),
            above=above,
            below_count=len(active) - len(above),
            fetched=FetchedCounts(
                sam=len(raw.sam.records) if raw.sam else 0, grants_gov=len(raw.gg_hits)
            ),
            skipped_records=skipped,
        )

    def summarize_dry_run(self, data: Prepared) -> str:
        raw = data.raw
        sam = raw.sam
        lines = [
            "",
            f"SAM window: {sam.window_from}..{sam.window_to}, {len(sam.records)} records "
            f"(total {sam.total_records}, complete={sam.complete})"
            if sam
            else "SAM: skipped (no SAM_API_KEY)",
            f"SAM requests today: {raw.state.sam.requests_today(raw.today)}"
            f" (budget left {raw.sam_budget.remaining() if raw.sam_budget else 'n/a'})",
            f"Grants.gov: {len(raw.gg_hits)} unique hits, {raw.gg_detail_fetches} details "
            f"fetched{' (ERROR: ' + raw.gg_error + ')' if raw.gg_error else ''}",
            f"Fetched: sam={data.fetched.sam} grants_gov={data.fetched.grants_gov} "
            f"(skipped unparseable/unsupported: {data.skipped_records})",
            f"Active after hard filters: {len(data.active)} "
            f"(new {len(data.new_ids)}, changed {len(data.changed_ids)})",
            "Rejected by reason:",
            *[f"  {reason:<18} {n}" for reason, n in data.rejected.items()],
            f"At/above relevance {raw.config.settings.relevance_threshold}: {len(data.above)}; "
            f"below: {data.below_count}",
            f"Top {raw.config.settings.top_n_summaries} by relevance (no LLM calls):",
        ]
        for id_ in data.above[: raw.config.settings.top_n_summaries]:
            lines.append(f"  [{data.relevance[id_]:3d}] {id_:40s} {data.active[id_].title[:70]}")
        return "\n".join(lines)

    # ---- analyze ------------------------------------------------------------------

    def analyze(self, ctx: RunContext, data: Prepared) -> AgentResult:
        raw = data.raw
        profile, config, today, now = raw.profile, raw.config, raw.today, raw.now
        p_hash = profile_hash(profile)
        score_model, summary_model = scoring.model_id(), summarize.model_id()
        active = dict(data.active)
        relevance = dict(data.relevance)

        if profile_changed(raw.state, p_hash):
            log.info("profile changed: rescoring up to %d items", len(data.above))

        # 1. cached scores/summaries (§5.4 / §7.3 cache keys)
        scores: dict[str, Score] = {}
        summaries: dict[str, Summary] = {}
        for id_, opp in active.items():
            prior = raw.store.get(id_)
            if prior is None or raw.options.rescore_all:
                continue
            if scoring.cache_key_matches(prior.score, opp, profile_hash=p_hash, model=score_model):
                scores[id_] = scoring.refinalize(prior.score, opp, profile, config)
            if summarize.cache_key_matches(
                prior.summary, opp, profile_hash=p_hash, model=summary_model
            ):
                summaries[id_] = prior.summary
        cached = len(scores)

        # 2. score the cache misses among the candidates, capped (§5.3/§5.4)
        to_score = [active[id_] for id_ in data.above if id_ not in scores]
        to_score = to_score[: config.settings.max_llm_scoring_per_run]
        run = scoring.score_opportunities(
            ctx.llm, to_score, profile, config, profile_hash=p_hash, today=today, now=now
        )
        scores.update(run.scores)
        scored_this_run = run.scored
        log.info(
            "scoring: %d cached, %d scored (%s), %d failed/deferred",
            cached,
            run.scored,
            run.mode,
            len(run.failed),
        )

        # 3. top N, then budgeted SAM description fetches + rescore on change (§5.5)
        top_n = config.settings.top_n_summaries
        top_ids = rank_top(active, scores, top_n)
        rescored = self._fetch_top_descriptions(
            ctx, raw, active, relevance, scores, top_ids, p_hash
        )
        scored_this_run += rescored
        top_ids = rank_top(active, scores, top_n)

        # 4. summaries for top-N entries without a valid cached one (§5.5 #3, §7.3)
        uncacheable: set[str] = set()
        for id_ in top_ids:
            if id_ in summaries:
                continue
            summary, cacheable = summarize.summarize_one(
                ctx.llm, active[id_], scores[id_], profile, profile_hash=p_hash,
                today=today, now=now,
            )
            summaries[id_] = summary
            if not cacheable:
                uncacheable.add(id_)

        # 5. persist store + state (only on a full run; dry runs never reach here)
        entries = {
            id_: StoreEntry(
                opportunity=opp,
                relevance=relevance[id_],
                score=scores.get(id_),
                summary=summaries.get(id_) if id_ not in uncacheable else None,
            )
            for id_, opp in active.items()
        }
        store_bytes = save_store(entries, store_path())
        self._save_state(raw, p_hash)

        # 6. documents
        sam_state = raw.state.sam
        facts = RunFacts(
            now=now,
            today=today,
            profile=profile,
            profile_hash=p_hash,
            config=config,
            active=active,
            relevance=relevance,
            scores=scores,
            summaries=summaries,
            top_ids=top_ids,
            new_ids=data.new_ids,
            changed_ids=data.changed_ids,
            fetched=data.fetched,
            rejected=data.rejected,
            below_relevance=data.below_count,
            llm_scored_this_run=scored_this_run,
            llm_scored_cached=cached,
            sam_budget_exhausted=bool(
                (raw.sam and raw.sam.budget_exhausted)
                or (raw.sam_budget and raw.sam_budget.exhausted)
            ),
            sam_requests_used=sam_state.requests_today(today),
        )
        docs = build_documents(facts)
        log.info(
            "published %d active (%d matches), top %d, store %d bytes",
            len(active),
            docs.active_matches,
            len(top_ids),
            store_bytes,
        )
        sources_ok = [raw.sam is not None and raw.sam.error is None, raw.gg_error is None]
        return AgentResult(
            body=docs.body,
            sources=[
                Source(name="SAM.gov Contract Opportunities", url="https://sam.gov/",
                       retrieved_at=now),
                Source(name="Grants.gov", url="https://www.grants.gov/", retrieved_at=now),
            ],
            headline=docs.headline,
            key_stats=docs.key_stats,
            data_changed=bool(
                data.new_ids or data.changed_ids or scored_this_run
                or any(summaries[i].generated_at == now for i in top_ids)
            ),
            items_count=docs.active_matches,
            files={"all.json": docs.all_json},
            status="ok" if any(sources_ok) else "stale",
        )

    def _fetch_top_descriptions(
        self,
        ctx: RunContext,
        raw: RawFetch,
        active: dict[str, Opportunity],
        relevance: dict[str, int],
        scores: dict[str, Score],
        top_ids: list[str],
        p_hash: str,
    ) -> int:
        """§5.5 #2: top-N SAM items without a description, in fit order, within
        the remaining SAM budget and the daily `max_sam_description_fetches`.
        Rescores (one sync fast call) any item whose content hash changed.
        Returns the number of items rescored."""
        budget, api_key = raw.sam_budget, raw.sam_api_key
        if budget is None or not api_key:
            return 0
        sam_state = raw.state.sam
        cap = raw.config.settings.max_sam_description_fetches
        records = {r.get("noticeId"): r for r in (raw.sam.records if raw.sam else [])}
        rescored = 0
        wanted = [i for i in top_ids if active[i].source == "sam"
                  and not active[i].description_fetched]
        for id_ in wanted:
            if sam_state.description_fetches_today(raw.today) >= cap:
                log.info("description fetches: daily cap of %d reached", cap)
                break
            if budget.remaining() <= 0:
                budget.exhausted = True
                log.info("description fetches: SAM budget used up")
                break
            opp = active[id_]
            url = (records.get(opp.source_id) or {}).get("description") or ""
            if not url.startswith(f"https://{SAM_HOST}/"):
                url = sam_description_url(opp.source_id)
            try:
                text = fetch_description(budget, api_key, url)
            except RequestBudgetExceeded:
                save_state(raw.state, state_path())
                break
            except HttpError as e:
                sam_state.record_description_fetch(raw.today)
                save_state(raw.state, state_path())
                log.warning("description %s failed: %s", id_, e)
                continue
            sam_state.record_description_fetch(raw.today)
            save_state(raw.state, state_path())
            updated = with_sam_description(opp, text)
            active[id_] = updated
            relevance[id_] = compute_relevance(updated, raw.profile)
            if updated.content_hash != opp.content_hash:
                run = scoring.score_opportunities(
                    ctx.llm, [updated], raw.profile, raw.config, profile_hash=p_hash,
                    today=raw.today, now=raw.now, use_batch=False,
                )
                if updated.id in run.scores:
                    scores[updated.id] = run.scores[updated.id]
                    rescored += 1
                else:
                    scores.pop(updated.id, None)  # stale: content changed, rescore next run
        return rescored

    def _save_state(self, raw: RawFetch, p_hash: str) -> None:
        state = raw.state
        if raw.sam is not None and raw.sam.complete:
            state.sam.last_posted_to = raw.sam.window_to
        if raw.gg_error is None and raw.profile.include_grants:
            state.grants_gov.last_run = raw.now.strftime("%Y-%m-%dT%H:%M:%SZ")
        state.profile_hash = p_hash
        state.prompt_versions.score = scoring.SCORE_PROMPT_VERSION
        state.prompt_versions.summary = summarize.SUMMARY_PROMPT_VERSION
        state.sam.trim(raw.today)
        save_state(state, state_path())


AGENT = GrantsAgent()
