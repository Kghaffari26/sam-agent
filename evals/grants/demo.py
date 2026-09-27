"""Offline demo of bid research: replay a recorded agent-loop trajectory.

    uv run python -m evals.grants.demo [case-id]      # default: r1-va-dashboard

Runs the production research loop (tools, guard, budgets) against the fixture
world with agents-core's `ReplayClient` answering the model calls from
`evals/grants/trajectories/<case-id>.json` (recorded by the last live eval run),
then prints the trajectory and the brief that would be published as
`top_matches[i].research`. No network, no keys, no cost.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from agents_core.agent_loop import ReplayClient
from agents_core.costs import CostTracker
from agents_core.llm import LLM

from evals.grants import research_world

TRAJECTORIES = Path(__file__).resolve().parent / "trajectories"


def replay(case_id: str, workdir: Path, trajectory: Path | None = None):
    case = next(c for c in research_world.cases() if c.id == case_id)
    tracker = CostTracker(agent="demo", run_id="demo", max_usd=1.0,
                          path=workdir / "costs.jsonl")
    client = ReplayClient(trajectory or TRAJECTORIES / f"{case_id}.json")
    return research_world.run_research(LLM(tracker, client=client), case, workdir)


def main() -> None:
    case_id = sys.argv[1] if len(sys.argv) > 1 else "r1-va-dashboard"
    with tempfile.TemporaryDirectory() as tmp:
        outcome = replay(case_id, Path(tmp))
    loop, b = outcome.loop, outcome.block
    print(f"{case_id}: stop={loop.stop_reason} steps={loop.steps} guard={b.narrative_source}")
    for call in loop.tool_calls:
        print(f"  step {call.step}: {call.tool}({', '.join(f'{k}={v!r}' for k, v in call.input.items())})")
    print(f"\nWhat they're buying: {b.what_theyre_buying}")
    print("Evaluation criteria: " + "; ".join(b.evaluation_criteria))
    print(f"Likely incumbent: {b.likely_incumbent}  -- {b.incumbent_notes}")
    for a in b.prior_awards:
        print(f"  prior award {a.award_id}: {a.recipient}, ${a.amount:,.2f} ({a.url})")
    print("Risks: " + "; ".join(b.risks))
    print(f"Go/no-go: {b.go_no_go.upper()} -- {b.rationale}")


if __name__ == "__main__":
    main()
