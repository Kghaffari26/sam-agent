"""Test doubles: an httpx transport serving recorded fixtures, and a fake
Anthropic client for `agents_core.llm` (no live network calls in tests)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "grants"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


# ---- HTTP ---------------------------------------------------------------------------


class FixtureServer:
    """Routes SAM.gov / Grants.gov requests to fixtures and records every call."""

    def __init__(
        self,
        *,
        sam_pages: list[dict[str, Any]] | None = None,
        sam_status: int = 200,
        sam_descriptions: dict[str, str] | None = None,
        gg_search: dict[str, Any] | None = None,
        gg_details: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.sam_pages = sam_pages or []
        self.sam_status = sam_status
        self.sam_descriptions = sam_descriptions or {}
        self.gg_search = gg_search
        self.gg_details = gg_details or {}
        self.calls: list[httpx.Request] = []

    def calls_to(self, host: str, path_part: str = "") -> list[httpx.Request]:
        return [r for r in self.calls if r.url.host == host and path_part in r.url.path]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = request.url
        if url.host == "api.sam.gov":
            if url.path.endswith("/v2/search"):
                if self.sam_status != 200:
                    return httpx.Response(self.sam_status, content=b"")
                offset = int(url.params.get("offset", 0))
                index = offset // 1000
                if index >= len(self.sam_pages):
                    return httpx.Response(200, json={"totalRecords": 0, "opportunitiesData": []})
                return httpx.Response(200, json=self.sam_pages[index])
            notice_id = url.params.get("noticeid")
            if notice_id is None and url.path.endswith("/description"):
                notice_id = url.path.split("/")[-2]  # the v2 path-style description URL
            if notice_id in self.sam_descriptions:
                return httpx.Response(200, json={"description": self.sam_descriptions[notice_id]})
            return httpx.Response(404, content=b"")
        if url.host == "api.grants.gov":
            body = json.loads(request.content or b"{}")
            if url.path.endswith("/search2"):
                if self.gg_search is None:
                    return httpx.Response(200, json={"errorcode": 0, "data": {}})
                return httpx.Response(200, json=self.gg_search)
            if url.path.endswith("/fetchOpportunity"):
                detail = self.gg_details.get(str(body.get("opportunityId")))
                if detail is None:
                    return httpx.Response(404, content=b"")
                return httpx.Response(200, json=detail)
        return httpx.Response(599, content=b"unexpected host")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


# ---- Anthropic -----------------------------------------------------------------------

Responder = Callable[[str, str], dict[str, Any]]


def default_rubric(prompt: str) -> dict[str, Any]:
    """Deterministic 'model': strong fit for IT-looking titles, weak otherwise."""
    payload = json.loads(prompt)
    title = payload["opportunity"]["title"].lower()
    it = any(k in title for k in ("cloud", "software", "data", "modern", "ai", "cyber"))
    return {
        "capability": 34 if it else 8,
        "eligibility": 18 if payload["eligibility_facts"]["set_aside_ok"] else 2,
        "size": 8,
        "timeline": 12,
        "strategic": 7 if it else 2,
        "reasons": ["Software modernization scope"] if it else ["Outside core capability"],
        "red_flags": [],
        "confidence": "medium",
    }


def default_summary(prompt: str) -> dict[str, Any]:
    payload = json.loads(prompt)
    opp = payload["opportunity"]
    return {
        "what_they_want": f"{opp['agency'] or 'The agency'} wants help with {opp['title']}.",
        "why_fit": ["Matches core software work"],
        "risks": ["Scope details are in the attachments"],
        "next_steps": ["Read the notice and attachments", "Confirm SAM registration"],
    }


def _message(text: str, parsed: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=150,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        stop_reason="end_turn",
        parsed_output=parsed,
    )


def _system_text(params: dict[str, Any]) -> str:
    return "\n".join(b["text"] for b in params["system"])


class FakeClient:
    """Mimics the SDK surface agents_core.llm uses: messages.create/parse and
    messages.batches.create/retrieve/results/cancel."""

    def __init__(
        self,
        rubric: Callable[[str], dict[str, Any]] = default_rubric,
        summary: Callable[[str], dict[str, Any]] = default_summary,
        *,
        batch_polls: int = 0,
    ) -> None:
        self.rubric = rubric
        self.summary = summary
        self.batch_polls = batch_polls
        self.sync_calls: list[dict[str, Any]] = []
        self.batch_requests: list[list[dict[str, Any]]] = []
        self._batches: dict[str, list[dict[str, Any]]] = {}
        self._polls: dict[str, int] = {}
        self.cancelled: list[str] = []
        self.messages = SimpleNamespace(
            create=self._create,
            parse=self._parse,
            batches=SimpleNamespace(
                create=self._batch_create,
                retrieve=self._batch_retrieve,
                results=self._batch_results,
                cancel=self._batch_cancel,
            ),
        )

    @property
    def total_calls(self) -> int:
        return len(self.sync_calls) + sum(len(b) for b in self.batch_requests)

    def _answer(self, params: dict[str, Any]) -> dict[str, Any]:
        prompt = params["messages"][0]["content"]
        if "bid/no-bid" in _system_text(params):
            return self.summary(prompt)
        return self.rubric(prompt)

    def _create(self, **params: Any) -> SimpleNamespace:
        self.sync_calls.append(params)
        return _message(json.dumps(self._answer(params)))

    def _parse(self, output_format: Any, **params: Any) -> SimpleNamespace:
        self.sync_calls.append(params)
        data = self._answer(params)
        return _message(json.dumps(data), parsed=output_format.model_validate(data))

    def _batch_create(self, requests: list[dict[str, Any]]) -> SimpleNamespace:
        batch_id = f"batch_{len(self.batch_requests) + 1}"
        self.batch_requests.append(list(requests))
        self._batches[batch_id] = list(requests)
        self._polls[batch_id] = 0
        return SimpleNamespace(id=batch_id)

    def _batch_retrieve(self, batch_id: str) -> SimpleNamespace:
        self._polls[batch_id] += 1
        ended = self._polls[batch_id] > self.batch_polls
        return SimpleNamespace(processing_status="ended" if ended else "in_progress")

    def _batch_results(self, batch_id: str) -> list[SimpleNamespace]:
        out = []
        for req in self._batches[batch_id]:
            text = json.dumps(self._answer(req["params"]))
            out.append(
                SimpleNamespace(
                    custom_id=req["custom_id"],
                    result=SimpleNamespace(type="succeeded", message=_message(text)),
                )
            )
        return out

    def _batch_cancel(self, batch_id: str) -> None:
        self.cancelled.append(batch_id)
