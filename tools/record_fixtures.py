"""Record live API responses into tests/fixtures/grants/ (a dev tool, not a test).

    uv run python -m tools.record_fixtures grants-gov
    uv run python -m tools.record_fixtures sam-from-cache

`grants-gov` calls the free Grants.gov API. `sam-from-cache` never calls
SAM.gov: it copies the SAM search response agents-core's HTTP cache already
holds from a real run (.cache/http/api.sam.gov/) and trims it to ~50 notices,
so recording a SAM fixture costs no extra request against the ~10/day key.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from agents_core.http import Http

from agents.grants.fetch_grants_gov import DETAIL_URL, HEADERS, SEARCH_URL

FIXTURES = Path("tests/fixtures/grants")
SAM_CACHE = Path(".cache/http/api.sam.gov")
SAM_TRIM = 50


def _write(name: str, data: object) -> None:
    path = FIXTURES / name
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {path} ({path.stat().st_size} bytes)")


def record_grants_gov() -> None:
    with Http() as http:
        body = {"keyword": "software", "oppStatuses": "forecasted|posted", "rows": 25}
        search = http.request("POST", SEARCH_URL, json_body=body, headers=HEADERS, ttl_seconds=0)
        data = search.json()
        data.pop("token", None)
        _write("grants_gov_search2_live.json", data)
        hits = data["data"]["oppHits"]
        posted = next(h for h in hits if h["oppStatus"] == "posted")
        forecast = next((h for h in hits if h["oppStatus"] == "forecasted"), None)
        for label, hit in (("posted", posted), ("forecasted", forecast)):
            if hit is None:
                continue
            r = http.request(
                "POST", DETAIL_URL, json_body={"opportunityId": int(hit["id"])},
                headers=HEADERS, ttl_seconds=0,
            )
            detail = r.json()
            detail.pop("token", None)
            _write(f"grants_gov_fetch_opportunity_live_{label}.json", detail)


def record_sam_from_cache() -> None:
    newest = None
    for path in sorted(SAM_CACHE.glob("*.json"), key=lambda p: p.stat().st_mtime):
        entry = json.loads(path.read_text())
        if "/v2/search" in entry["url"]:
            newest = entry
    if newest is None:
        sys.exit("no cached SAM search response; run the agent once first")
    data = json.loads(base64.b64decode(newest["content_b64"]))
    records = data.get("opportunitiesData") or []
    data["opportunitiesData"] = records[:SAM_TRIM]
    data["_fixture_note"] = (
        f"Recorded from a live SAM.gov v2 search ({newest['url']}), trimmed from "
        f"{len(records)} to {len(data['opportunitiesData'])} notices. totalRecords is the "
        "live value."
    )
    data.pop("links", None)
    for record in data["opportunitiesData"]:  # public, but no need to commit people's details
        for i, contact in enumerate(record.get("pointOfContact") or []):
            for key, value in (("fullName", f"Redacted Contact {i + 1}"),
                               ("email", "redacted@example.gov"), ("phone", "000-000-0000"),
                               ("fax", "000-000-0000"), ("title", "Redacted")):
                if contact.get(key):
                    contact[key] = value
    data["_fixture_note"] += " pointOfContact names/emails/phones redacted."
    _write("sam_search_live.json", data)


if __name__ == "__main__":
    {"grants-gov": record_grants_gov, "sam-from-cache": record_sam_from_cache}[sys.argv[1]]()
