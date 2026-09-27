"""The fixture world for bid-research evals and replay tests: five opportunities
(`research/cases.json`) and an httpx transport that serves their SAM attachments
(`research/files/`), USAspending.gov award searches (`research/usaspending.json`,
routed by awarding agency) and Grants.gov details (`research/grants_gov/`).

The agent loop, tools, guard and budgets are the production ones
(`agents.grants.research`); only the network is replaced. USAspending.gov and
sam.gov attachment downloads aren't reachable from the environment this was built
in, so the responses are hand-built to the documented shapes (see STATUS.md).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
from agents_core.evals import EvalCase, load_cases
from agents_core.http import Http
from agents_core.llm import LLM

from agents.grants.config import (
    BusinessProfile,
    ResearchSettings,
    load_business_profile,
    load_grants_config,
)
from agents.grants.fetch_sam import SamBudget
from agents.grants.models import Opportunity, Score
from agents.grants.research import ResearchEnv, ResearchOutcome, research_one
from agents.grants.state import SamState

HERE = Path(__file__).resolve().parent
WORLD = HERE / "research"
REPO_ROOT = HERE.parents[1]
AS_OF = date(2026, 9, 27)
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def cases() -> list[EvalCase]:
    return load_cases(WORLD / "cases.json")


def case_objects(case: EvalCase) -> tuple[Opportunity, Score]:
    return (
        Opportunity.model_validate(case.input["opportunity"]),
        Score.model_validate(case.input["score"]),
    )


def profile() -> BusinessProfile:
    return load_business_profile(REPO_ROOT / "config" / "business_profile.toml")


def research_settings() -> ResearchSettings:
    return load_grants_config(REPO_ROOT / "config" / "grants.toml").research


class World:
    """Serves the fixture responses and records every request."""

    def __init__(self) -> None:
        self.files = {p.stem: p.read_bytes() for p in (WORLD / "files").iterdir()}
        self.usaspending: dict[str, Any] = json.loads((WORLD / "usaspending.json").read_text())
        self.grants = {p.stem: json.loads(p.read_text())
                       for p in (WORLD / "grants_gov").glob("*.json")}
        self.calls: list[httpx.Request] = []

    def _awards(self, body: dict[str, Any]) -> dict[str, Any]:
        filters = body.get("filters") or {}
        names = " ".join(a.get("name", "") for a in filters.get("agencies") or []).lower()
        for key, response in self.usaspending.items():
            if key in names:
                return response
        return {"results": [], "page_metadata": {"page": 1, "hasNext": False}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = request.url
        if url.host == "sam.gov" and url.path.endswith("/download"):
            data = self.files.get(url.path.split("/")[-2])
            if data is None:
                return httpx.Response(404)
            return httpx.Response(200, content=data, headers={"etag": '"fixture"'})
        if url.host == "api.usaspending.gov":
            return httpx.Response(200, json=self._awards(json.loads(request.content or b"{}")))
        if url.host == "api.grants.gov" and url.path.endswith("/fetchOpportunity"):
            opp_id = str(json.loads(request.content or b"{}").get("opportunityId"))
            if opp_id in self.grants:
                return httpx.Response(200, json=self.grants[opp_id])
            return httpx.Response(404)
        return httpx.Response(599, content=b"unexpected host in the research fixture world")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def run_research(
    llm: LLM,
    case: EvalCase,
    workdir: Path,
    *,
    settings: ResearchSettings | None = None,
    world: World | None = None,
) -> ResearchOutcome:
    """One production research loop against the fixture world."""
    world = world or World()
    http = Http(cache_dir=workdir / "http", transport=world.transport(), sleep=lambda s: None)
    try:
        env = ResearchEnv(
            http=http,
            profile=profile(),
            today=AS_OF,
            settings=settings or research_settings(),
            sam_budget=SamBudget(http, SamState(), daily_budget=8, today=AS_OF),
            attachments_dir=workdir / "attachments",
        )
        opp, score = case_objects(case)
        return research_one(llm, env, opp, score, now=NOW)
    finally:
        http.close()
