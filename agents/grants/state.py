"""Run state persisted to `data/grants/state.json` (SPEC_GRANTS.md §4).

This tracks the SAM fetch window, per-day SAM request counts, the last
Grants.gov run, the profile hash last scored against, and prompt versions.
The per-day SAM budget is enforced by `agents_core.http`'s per-host request
budget together with this committed ledger (see `fetch_sam.SamBudget`); this
module owns the agent-specific bits that don't belong in shared code: the
fetch window, the ledger itself and the profile/prompt-version bookkeeping.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from pydantic import BaseModel, Field


class SamState(BaseModel):
    last_posted_to: date | None = None
    # Every SAM.gov request (searches + description fetches), per UTC day.
    requests: dict[str, int] = Field(default_factory=dict)
    # Description fetches alone, per UTC day, for `max_sam_description_fetches`.
    description_fetches: dict[str, int] = Field(default_factory=dict)
    # Set when SAM rejects the key (401/403, §10); cleared on the next success.
    key_rejected_at: str | None = None

    def description_fetches_today(self, today: date) -> int:
        return self.description_fetches.get(today.isoformat(), 0)

    def record_description_fetch(self, today: date) -> None:
        key = today.isoformat()
        self.description_fetches[key] = self.description_fetches.get(key, 0) + 1

    def trim(self, today: date, keep_days: int = 30) -> None:
        """Keep the ledgers from growing forever: drop days older than `keep_days`."""
        cutoff = (today - timedelta(days=keep_days)).isoformat()
        self.requests = {d: n for d, n in self.requests.items() if d >= cutoff}
        self.description_fetches = {
            d: n for d, n in self.description_fetches.items() if d >= cutoff
        }

    def requests_today(self, today: date) -> int:
        return self.requests.get(today.isoformat(), 0)

    def record_request(self, today: date, *, count: int = 1) -> None:
        key = today.isoformat()
        self.requests[key] = self.requests.get(key, 0) + count


class GrantsGovState(BaseModel):
    last_run: str | None = None


class PromptVersions(BaseModel):
    score: str = "v1"
    summary: str = "v1"


class GrantsState(BaseModel):
    sam: SamState = Field(default_factory=SamState)
    grants_gov: GrantsGovState = Field(default_factory=GrantsGovState)
    profile_hash: str | None = None
    prompt_versions: PromptVersions = Field(default_factory=PromptVersions)


def load_state(path: str | Path) -> GrantsState:
    p = Path(path)
    if not p.exists():
        return GrantsState()
    return GrantsState.model_validate(json.loads(p.read_text()))


def save_state(state: GrantsState, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")


def profile_changed(state: GrantsState, current_profile_hash: str) -> bool:
    """True if the profile has changed since the last run (SPEC_GRANTS.md §10):
    every cached score is invalidated and rescoring is due, budget permitting."""
    return state.profile_hash is not None and state.profile_hash != current_profile_hash
