"""SAM.gov Get Opportunities fetcher: budgeted window fetch, pagination and
description fetches (SPEC_GRANTS.md §3.1, §10).

All HTTP goes through `agents_core.http.Http`. SAM.gov's daily request cap is
enforced twice over:

1. `agents_core.http`'s per-host `daily_budget` (`HostPolicy`, set by
   `GrantsAgent` from `sam_daily_request_budget`), which counts requests actually
   sent per UTC day (never cache hits), with a single attempt per request (no
   retries on a budgeted host since agents-core v0.2.0); and
2. the committed ledger in `data/grants/state.json` (`sam.requests[date]`),
   which survives fresh CI checkouts where agents-core's own budget file
   (under `.cache/http`) might not.

`SamBudget` syncs the Http policy down to the smaller of the two,
`remaining()` reports it, and every network request made through it is
recorded in the ledger, so the ledger never undercounts. Cache hits are free
under both. Attachment downloads (bid research) go through
`SamBudget.download`, agents-core's conditional-GET `Http.download`, and are
counted in the same ledger.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from agents_core.http import (
    DownloadResult,
    HostPolicy,
    Http,
    HttpError,
    RequestBudgetExceeded,
    Response,
)

from agents.grants.state import SamState

log = logging.getLogger(__name__)

SAM_HOST = "api.sam.gov"
SAM_FILES_HOST = "sam.gov"  # attachment downloads (`resourceLinks`)
SEARCH_URL = "https://api.sam.gov/prod/opportunities/v2/search"
PAGE_LIMIT = 1000  # the API's maximum
MAX_WINDOW_DAYS = 364  # postedFrom/postedTo may span at most one year


def mdy(d: date) -> str:
    """SAM wants `MM/dd/yyyy`."""
    return d.strftime("%m/%d/%Y")


def sam_window(
    state: SamState,
    *,
    today: date,
    overlap_days: int,
    first_run_lookback_days: int,
    lookback_days: int | None = None,
) -> tuple[date, date]:
    """One window per run (§3.1 #2): from the last successful `postedTo`, minus
    `overlap_days`, to today. `lookback_days` overrides it (manual backfills)."""
    if lookback_days is not None:
        start = today - timedelta(days=lookback_days)
    elif state.last_posted_to is None:
        start = today - timedelta(days=first_run_lookback_days)
    else:
        start = state.last_posted_to - timedelta(days=overlap_days)
    start = max(start, today - timedelta(days=MAX_WINDOW_DAYS))
    return min(start, today), today


class SamBudget:
    """Gatekeeper for every SAM.gov request, see the module docstring."""

    def __init__(self, http: Http, state: SamState, *, daily_budget: int, today: date) -> None:
        self.http = http
        self.state = state
        self.daily_budget = daily_budget
        self.today = today
        self.used_this_run = 0
        self.exhausted = False
        self._sync_http_policy()

    def _sync_http_policy(self) -> None:
        """Tighten agents-core's own per-host cap to the committed ledger, so a
        fresh checkout (no `.cache/http` budget file) still stops at the ledger.
        One attempt per request: a 5xx must not burn a scarce SAM request twice."""
        http_left = self.http.budget_remaining(SAM_HOST)
        if http_left is None:
            http_used, http_left = 0, self.daily_budget
        else:
            policy = self.http.policies.get(SAM_HOST, HostPolicy())
            http_used = (policy.daily_budget or 0) - http_left
        ledger_left = max(self.daily_budget - self.state.requests_today(self.today), 0)
        policy = self.http.policies.get(SAM_HOST, HostPolicy())
        self.http.set_policy(
            SAM_HOST,
            HostPolicy(
                min_interval_seconds=policy.min_interval_seconds,
                daily_budget=max(http_used, 0) + min(ledger_left, http_left),
                max_attempts=1,
            ),
        )

    def remaining(self) -> int:
        ledger_left = self.daily_budget - self.state.requests_today(self.today)
        http_left = self.http.budget_remaining(SAM_HOST)
        left = ledger_left if http_left is None else min(ledger_left, http_left)
        return max(left, 0)

    def get(self, url: str, params: dict[str, Any]) -> Response:
        if urlsplit(url).netloc.lower() != SAM_HOST:
            raise ValueError(f"not a SAM.gov API URL: {urlsplit(url).netloc}")
        # No pre-check here: agents-core's Http enforces the (ledger-synced) per-host
        # budget on network attempts only, so a cached response is still served
        # free after the budget is used up.
        before_http = self.http.budget_remaining(SAM_HOST)
        before_net = self.http.network_requests
        try:
            return self.http.get(url, params=params)
        except RequestBudgetExceeded:
            self.exhausted = True
            raise
        finally:
            after_http = self.http.budget_remaining(SAM_HOST)
            if before_http is not None and after_http is not None:
                used = before_http - after_http
            else:
                used = self.http.network_requests - before_net
            if used > 0:
                self.state.record_request(self.today, count=used)
                self.used_this_run += used

    def download(self, url: str, dest: Path) -> DownloadResult:
        """Download one SAM attachment through agents-core's conditional-GET
        `Http.download` (ETag/Last-Modified sidecar next to `dest`). Costs one
        request of the day's SAM budget, recorded in the ledger even when it fails."""
        host = urlsplit(url).netloc.lower()
        if host not in (SAM_HOST, SAM_FILES_HOST):
            raise ValueError(f"not a SAM.gov URL: {host}")
        if self.remaining() <= 0:
            self.exhausted = True
            raise RequestBudgetExceeded(f"SAM.gov daily budget of {self.daily_budget} used up")
        before = self.http.network_requests
        try:
            return self.http.download(url, dest)
        finally:
            used = self.http.network_requests - before  # sent requests only
            if used > 0:
                self.state.record_request(self.today, count=used)
                self.used_this_run += used
                self._sync_http_policy()


@dataclass
class SamFetchResult:
    window_from: date
    window_to: date
    records: list[dict[str, Any]] = field(default_factory=list)
    total_records: int | None = None
    # Every page of the window came back: only then may `last_posted_to` advance.
    complete: bool = False
    empty_404: bool = False
    budget_exhausted: bool = False
    auth_failed: bool = False
    error: str | None = None


def _is_rate_limited(e: HttpError) -> bool:
    return e.status == 429 or "HTTP 429" in str(e)


def fetch_window(
    budget: SamBudget,
    api_key: str,
    *,
    window: tuple[date, date],
    ptypes: list[str],
) -> SamFetchResult:
    """Fetch every notice posted in `window`, paginating only if needed and only
    within budget. Never raises for SAM-side failures; see `SamFetchResult`."""
    start, end = window
    result = SamFetchResult(window_from=start, window_to=end)
    offset = 0
    while True:
        params: dict[str, Any] = {
            "api_key": api_key,
            "postedFrom": mdy(start),
            "postedTo": mdy(end),
            "limit": PAGE_LIMIT,
            "offset": offset,
        }
        if ptypes:
            # Comma-separated in one param (not repeated params); locked in by
            # tests/grants/test_fetch_sam.py and confirmed against the live API.
            params["ptype"] = ",".join(ptypes)
        try:
            response = budget.get(SEARCH_URL, params)
        except RequestBudgetExceeded:
            result.budget_exhausted = True
            log.warning("SAM budget exhausted at offset %d; window will catch up", offset)
            break
        except HttpError as e:
            if e.status == 404:
                # §10: may mean "no results" or a blocked region. Zero results,
                # and don't advance the window, so the next run retries it.
                log.warning("SAM search returned 404; treating as zero results")
                result.empty_404 = True
            elif e.status in (401, 403):
                log.error("SAM API key rejected (%s); SAM portion skipped", e.status)
                result.auth_failed = True
                result.error = f"SAM API key rejected ({e.status}); it may need renewal"
            elif _is_rate_limited(e):
                log.warning("SAM rate-limited (429); stopping SAM calls this run")
                result.budget_exhausted = True
            else:
                log.error("SAM search failed: %s", e)
                result.error = str(e)
            break

        data = response.json()
        page = data.get("opportunitiesData") or []
        result.total_records = int(data.get("totalRecords") or 0)
        result.records.extend(page)
        offset += len(page)
        if not page or offset >= result.total_records:
            result.complete = True
            break
    log.info(
        "SAM window %s..%s: %d records (total %s, complete=%s)",
        start,
        end,
        len(result.records),
        result.total_records,
        result.complete,
    )
    return result


def fetch_description(budget: SamBudget, api_key: str, url: str) -> str:
    """Fetch one notice description (§3.1 #4: costs one request). Returns the
    HTML/text, or "" when SAM has none (404 or an empty body). Raises
    `RequestBudgetExceeded` when the budget is used up and `HttpError` on other
    failures."""
    # httpx *replaces* a URL's query string when `params=` is given, so the
    # description URL's own query (e.g. `noticeid=`) must travel in params too.
    parts = urlsplit(url)
    params: dict[str, Any] = dict(parse_qsl(parts.query))
    params["api_key"] = api_key
    base = f"{parts.scheme}://{parts.netloc}{parts.path}"
    try:
        response = budget.get(base, params)
    except HttpError as e:
        if isinstance(e, RequestBudgetExceeded) or e.status != 404:
            raise
        return ""
    try:
        data = response.json()
    except ValueError:
        return response.text
    if isinstance(data, dict):
        return str(data.get("description") or "")
    return ""
