"""Per-tier sampling: the fast tier's temperature 0 (config/models.toml) reaches the
API on synchronous calls (agents-core >= v0.3.1 sends it in `extra_body`)."""

from types import SimpleNamespace

from agents_core.costs import CostTracker
from agents_core.llm import LLM
from pydantic import BaseModel

from tests.grants.fakes import _message


class Out(BaseModel):
    ok: bool


class StrictMessages:
    """Like anthropic 1.8: create/parse have no `temperature` keyword."""

    def __init__(self):
        self.calls = []

    def create(self, *, model, max_tokens, messages, system=None, extra_body=None, **rest):
        assert "temperature" not in rest
        self.calls.append(extra_body)
        return _message('{"ok": true}')

    def parse(self, *, output_format, model, max_tokens, messages, system=None,
              extra_body=None, **rest):
        assert "temperature" not in rest
        self.calls.append(extra_body)
        return _message('{"ok": true}', parsed=output_format(ok=True))


def test_fast_tier_temperature_travels_in_the_body(tmp_path):
    client = SimpleNamespace(messages=StrictMessages())
    tracker = CostTracker(agent="t", run_id="t", max_usd=1.0, path=tmp_path / "c.jsonl")
    llm = LLM(tracker, client=client)
    llm.structured("fast", "hi", Out, system="s")  # config/models.toml: fast temperature 0
    llm.complete("fast", "hi", system="s", temperature=0.2)
    llm.complete("smart", "hi", system="s")  # no temperature on the smart tier
    temps = [(body or {}).get("temperature") for body in client.messages.calls]
    assert temps == [0, 0.2, None]
    assert tracker.calls == 3
