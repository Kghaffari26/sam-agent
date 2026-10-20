"""Set the agent up for a new contractor from its dashboard intake (SPEC_GRANTS.md §2.1).

    uv run python -m tools.setup_contractor config/intakes/acme.json
        writes config/profiles/<id>.toml: the recommended profile, filters and
        scoring, each with its reason as a comment
    ... --out PATH         write somewhere else
    ... --activate         also point config/grants.toml `[settings] profile` at it
    ... --json             print {profile, trades, rationale} as JSON instead (for the
                           dashboard to show and let the contractor adjust)
    uv run python -m tools.setup_contractor --schema
        print the intake's JSON Schema (to build the dashboard form from)

No network calls and no LLM cost. Intake files are JSON or TOML.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

from agents.grants.config import load_business_profile
from agents.grants.recommend import ContractorIntake, recommend_profile, to_toml

GRANTS_TOML = Path("config/grants.toml")
PROFILES_DIR = Path("config/profiles")


def load_intake(path: Path) -> ContractorIntake:
    if path.suffix == ".toml":
        with open(path, "rb") as f:
            return ContractorIntake.model_validate(tomllib.load(f))
    return ContractorIntake.model_validate_json(path.read_text())


def activate(profile_path: Path, grants_toml: Path = GRANTS_TOML) -> None:
    text = grants_toml.read_text()
    new, n = re.subn(r'(?m)^profile\s*=\s*".*"$', f'profile = "{profile_path.as_posix()}"', text)
    if n != 1:
        raise SystemExit(f"couldn't find one `profile = ...` line in {grants_toml}")
    grants_toml.write_text(new)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.setup_contractor")
    parser.add_argument("intake", nargs="?", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--schema", action="store_true")
    args = parser.parse_args(argv)

    if args.schema:
        print(json.dumps(ContractorIntake.model_json_schema(), indent=1))
        return 0
    if args.intake is None:
        parser.error("an intake file is required (or --schema)")
    rec = recommend_profile(load_intake(args.intake))
    if args.json:
        print(json.dumps(rec.to_json(), indent=1))
        return 0
    out = args.out or PROFILES_DIR / f"{rec.profile.id}.toml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_toml(rec))
    load_business_profile(out)  # what we wrote must load back
    print(f"wrote {out} (trades: {', '.join(rec.trades) or 'none recognized'})")
    if args.activate:
        activate(out)
        print(f"{GRANTS_TOML} now uses {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
