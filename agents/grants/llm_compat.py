"""Local workaround (see STATUS.md "Needed from agents-core"): per-tier `temperature`
on synchronous calls.

agents-core v0.3.0 sends a tier's `temperature` as a keyword argument to
`client.messages.create`/`messages.parse`, but the Anthropic SDK it pins (1.8) has
no such keyword, so every synchronous call on a tier with a temperature raises
`TypeError` (batches are unaffected: their params travel as JSON). The Messages API
itself still accepts `temperature` for the fast tier's model, so this shim moves it
into `extra_body`. It wraps the SDK client agents-core already built (no `anthropic`
import here) and returns an `LLM` on the same CostTracker, so MAX_RUN_USD, cost
logging and tracing are unchanged. Drop it once agents-core sends the parameter in
a form its SDK accepts.
"""

from __future__ import annotations

from typing import Any

from agents_core.llm import LLM


def _move_temperature(params: dict[str, Any]) -> dict[str, Any]:
    if "temperature" not in params:
        return params
    params = dict(params)
    temperature = params.pop("temperature")
    params["extra_body"] = {**(params.get("extra_body") or {}), "temperature": temperature}
    return params


class _Messages:
    def __init__(self, messages: Any) -> None:
        self._messages = messages

    def create(self, **params: Any) -> Any:
        return self._messages.create(**_move_temperature(params))

    def parse(self, **params: Any) -> Any:
        return self._messages.parse(**_move_temperature(params))

    def __getattr__(self, name: str) -> Any:  # batches, count_tokens, ...
        return getattr(self._messages, name)


class SamplingShim:
    """An SDK client whose sync message calls carry `temperature` in the body."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.messages = _Messages(client.messages)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def sampling_llm(llm: LLM) -> LLM:
    """`llm` with the shim applied (idempotent). Raises RuntimeError without a key,
    like `llm.client`."""
    client = llm.client
    if isinstance(client, SamplingShim):
        return llm
    return LLM(llm.tracker, client=SamplingShim(client), max_concurrency=llm.max_concurrency)
