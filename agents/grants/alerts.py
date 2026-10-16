"""Email digest of strong new matches, to the owner only (`[alerts]` in
`config/grants.toml`).

    uv run python -m agents.grants.alerts [--dry-run]

Run by the `alerts` job of `.github/workflows/agent-grants.yml` after a successful
agent run, on the store that run committed (`data/grants/store.json.gz`). It selects
active opportunities with fit >= `min_fit` and a recommendation in
`recommendations` that haven't been alerted at their current content hash (new
matches, items a rescore lifted over the threshold, and amended notices), best fit
first, at most `max_per_day` a UTC day, and sends ONE digest (plain text + simple
HTML, no tracking) through Resend's HTTP API to `ALERT_EMAIL_TO`. What was sent is
recorded in `data/grants/alerts.json` by `notice_id` + content hash, so nothing
repeats; an item over the day's cap goes out the next day.

Never fails the job: missing secrets or a delivery error are recorded as
`last_status` (and the next agent run publishes a `meta.warnings` line about it).
`--dry-run` writes the digest to the job summary (`$GITHUB_STEP_SUMMARY`, else
stdout) and sends and records nothing.

The reusable run-agent.yml passes the agent step a fixed list of secrets, so the
digest can't be sent from inside the agent run (see STATUS.md, "Needed from
agents-core").
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from agents_core.http import HostPolicy, Http, HttpError
from pydantic import BaseModel, Field

from agents.grants.config import AlertSettings, load_grants_config
from agents.grants.store import StoreEntry, load_store

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
RESEND_HOST = "api.resend.com"
KEY_ENV, TO_ENV = "ALERT_EMAIL_API_KEY", "ALERT_EMAIL_TO"
CONFIG_PATH = Path("config/grants.toml")
SKIPPED_NO_SECRETS = f"skipped: {KEY_ENV} or {TO_ENV} is not set"


class Alerted(BaseModel):
    content_hash: str
    fit: int
    alerted_at: str


class AlertState(BaseModel):
    """`data/grants/alerts.json`: what was sent (by notice id + content hash), how
    many per UTC day, and how the last attempt went."""

    alerted: dict[str, Alerted] = Field(default_factory=dict)
    sent_per_day: dict[str, int] = Field(default_factory=dict)
    last_run_at: str | None = None
    last_status: str | None = None

    def trim(self, today: date, keep_days: int = 30) -> None:
        self.sent_per_day = {d: n for d, n in self.sent_per_day.items()
                             if (today - date.fromisoformat(d)).days <= keep_days}


def state_path() -> Path:
    from agents.grants.agent import grants_data_dir

    return grants_data_dir() / "alerts.json"


def load_state(path: Path) -> AlertState:
    if not path.is_file():
        return AlertState()
    return AlertState.model_validate_json(path.read_text())


def save_state(state: AlertState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.model_dump_json(indent=2) + "\n")


# ---- selection ----------------------------------------------------------------------


@dataclass
class Entry:
    id: str
    title: str
    agency: str
    fit: int
    recommendation: str
    deadline: datetime | None
    days_left: int | None
    value: str
    set_aside: str
    reasons: list[str]
    source_url: str
    source_name: str
    site_url: str | None
    content_hash: str
    updated: bool  # alerted before at an older content hash


def _value(opp) -> str:
    amount, floor = opp.value_amount, opp.value_floor
    if amount is None:
        return "not stated"
    if floor is not None and floor != amount:
        return f"${floor:,.0f} - ${amount:,.0f}"
    label = {"award_ceiling": "up to ", "estimated_total": "est. total "}.get(opp.value_kind, "")
    return f"{label}${amount:,.0f}"


def _agency(opp) -> str:
    agency = opp.agency or "Unknown agency"
    sub = opp.sub_agency
    return f"{agency} / {sub}" if sub and sub != opp.agency else agency


def select(store: dict[str, StoreEntry], state: AlertState, settings: AlertSettings, *,
           today: date) -> list[Entry]:
    """Matches to alert now: over the threshold, not alerted at this content hash,
    still open, best fit first, within what's left of today's cap."""
    left = settings.max_per_day - state.sent_per_day.get(today.isoformat(), 0)
    if left <= 0:
        return []
    out = []
    for id_, e in store.items():
        s, opp = e.score, e.opportunity
        if s is None or s.fit < settings.min_fit or s.recommendation not in (
                settings.recommendations):
            continue
        if opp.deadline is not None and opp.deadline.date() < today:
            continue
        prior = state.alerted.get(id_)
        if prior is not None and prior.content_hash == opp.content_hash:
            continue
        summary = e.summary
        reasons = (summary.why_fit if summary and summary.content_hash == opp.content_hash
                   else []) or s.reasons
        out.append(Entry(
            id=id_, title=opp.title, agency=_agency(opp),
            fit=s.fit, recommendation=s.recommendation, deadline=opp.deadline,
            days_left=None if opp.deadline is None else (opp.deadline.date() - today).days,
            value=_value(opp), set_aside=opp.set_aside_label or "None",
            reasons=reasons[:2], source_url=str(opp.url),
            source_name="SAM.gov" if opp.source == "sam" else "Grants.gov",
            site_url=f"{settings.site_url.rstrip('/')}#{id_}" if settings.site_url else None,
            content_hash=opp.content_hash, updated=prior is not None,
        ))
    out.sort(key=lambda x: (-x.fit, x.days_left if x.days_left is not None else 10**6, x.id))
    return out[:left]


# ---- rendering ----------------------------------------------------------------------

UNSUBSCRIBE = ("You get this because [alerts] enabled = true in config/grants.toml of"
               " the sam-agent repo. To stop these emails, set enabled = false there.")


def _closing(e: Entry) -> str:
    if e.deadline is None:
        return "no deadline stated"
    when = e.deadline.strftime("%B %d, %Y").replace(" 0", " ")
    return f"{when} ({e.days_left} days left)" if e.days_left else f"{when} (today)"


def subject(entries: list[Entry]) -> str:
    n = len(entries)
    best = max(e.fit for e in entries)
    return (f"Grants Finder: {n} strong new match{'es' if n != 1 else ''}"
            f" (best fit {best})")


def render_text(entries: list[Entry], *, today: date) -> str:
    lines = [f"{len(entries)} strong match{'es' if len(entries) != 1 else ''} on"
             f" {today.strftime('%B %d, %Y').replace(' 0', ' ')}:", ""]
    for i, e in enumerate(entries, start=1):
        tag = " [updated notice]" if e.updated else ""
        lines += [
            f"{i}. {e.title}{tag}",
            f"   {e.agency}",
            f"   Fit {e.fit} ({e.recommendation}) | Closes {_closing(e)}",
            f"   Value: {e.value} | Set-aside: {e.set_aside}",
            *(f"   - {r}" for r in e.reasons),
            f"   {e.source_name}: {e.source_url}",
        ]
        if e.site_url:
            lines.append(f"   On the site: {e.site_url}")
        lines.append("")
    lines += ["--", UNSUBSCRIBE]
    return "\n".join(lines) + "\n"


def render_html(entries: list[Entry], *, today: date) -> str:
    esc = html.escape
    items = []
    for e in entries:
        tag = " <em>(updated notice)</em>" if e.updated else ""
        links = [f'<a href="{esc(e.source_url)}">{esc(e.source_name)}</a>']
        if e.site_url:
            links.append(f'<a href="{esc(e.site_url)}">On the site</a>')
        reasons = "".join(f"<li>{esc(r)}</li>" for r in e.reasons)
        items.append(
            f"<li style=\"margin-bottom:16px\"><strong>{esc(e.title)}</strong>{tag}<br>"
            f"{esc(e.agency)}<br>"
            f"Fit <strong>{e.fit}</strong> ({esc(e.recommendation)}) &middot; Closes"
            f" {esc(_closing(e))}<br>"
            f"Value: {esc(e.value)} &middot; Set-aside: {esc(e.set_aside)}"
            f"<ul>{reasons}</ul>{' &middot; '.join(links)}</li>")
    day = today.strftime("%B %d, %Y").replace(" 0", " ")
    return (
        "<!doctype html><html><body style=\"font-family:sans-serif;line-height:1.4\">"
        f"<p>{len(entries)} strong match{'es' if len(entries) != 1 else ''} on {day}:</p>"
        f"<ol>{''.join(items)}</ol>"
        f"<p style=\"color:#666;font-size:12px\">{esc(UNSUBSCRIBE)}</p></body></html>\n")


# ---- delivery -------------------------------------------------------------------


def send(http: Http, *, api_key: str, sender: str, to: str, subject_: str, text: str,
         html_: str) -> str:
    """POST one email to Resend; returns its id. Never cached, one attempt (a retry
    could send twice)."""
    http.set_policy(RESEND_HOST, HostPolicy(max_attempts=1))
    response = http.request(
        "POST", RESEND_URL, ttl_seconds=0,
        headers={"Authorization": f"Bearer {api_key}"},
        json_body={"from": sender, "to": [a.strip() for a in to.split(",") if a.strip()],
                   "subject": subject_, "text": text, "html": html_},
    )
    return str(response.json().get("id", ""))


def mark_sent(state: AlertState, entries: list[Entry], *, now: datetime) -> None:
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for e in entries:
        state.alerted[e.id] = Alerted(content_hash=e.content_hash, fit=e.fit, alerted_at=stamp)
    day = now.date().isoformat()
    state.sent_per_day[day] = state.sent_per_day.get(day, 0) + len(entries)


def summary_markdown(entries: list[Entry], subject_: str, text: str) -> str:
    return (f"## Email alert (dry run, not sent)\n\n**Subject:** {subject_}\n\n"
            f"```text\n{text}```\n" if entries else
            "## Email alert (dry run)\n\nNothing new to alert.\n")


def run(*, dry_run: bool, env: dict[str, str], now: datetime, store_path: Path,
        state_file: Path, settings: AlertSettings, http: Http | None = None,
        summary_path: Path | None = None) -> str:
    """One alerts pass; returns the status line (also saved as `last_status`, except
    on a dry run, which changes nothing)."""
    today = now.date()
    state = load_state(state_file)
    if not settings.enabled:
        return "disabled"
    entries = select(load_store(store_path), state, settings, today=today)
    subject_ = subject(entries) if entries else ""
    text = render_text(entries, today=today) if entries else ""
    if dry_run:
        out = summary_markdown(entries, subject_, text)
        if summary_path is not None:
            with summary_path.open("a") as f:
                f.write(out)
        else:
            sys.stdout.write(out)
        return f"dry run: {len(entries)} would be sent"

    if not env.get(KEY_ENV) or not env.get(TO_ENV):
        status = SKIPPED_NO_SECRETS
    elif not entries:
        status = "ok: nothing new"
    else:
        own = http is None
        http = http or Http()
        try:
            email_id = send(http, api_key=env[KEY_ENV], sender=settings.sender,
                            to=env[TO_ENV], subject_=subject_, text=text,
                            html_=render_html(entries, today=today))
            mark_sent(state, entries, now=now)
            status = f"ok: sent {len(entries)} ({email_id})"
        except (HttpError, ValueError) as e:
            status = f"failed: {e}"[:300]
        finally:
            if own:
                http.close()
    state.last_run_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    state.last_status = status
    state.trim(today)
    save_state(state, state_file)
    return status


def warning_for(state_file: Path, settings: AlertSettings) -> str | None:
    """A `meta.warnings` line for the agent run when the last alerts pass didn't
    deliver (the alerts job runs after the agent, so it shows one run later)."""
    if not settings.enabled:
        return None
    try:
        status = load_state(state_file).last_status
    except (OSError, ValueError):
        return None
    if status == SKIPPED_NO_SECRETS:
        return (f"Email alerts are enabled but weren't sent: set the {KEY_ENV} and {TO_ENV}"
                " repository secrets, or [alerts] enabled = false.")
    if status and status.startswith("failed"):
        return f"Email alerts failed on the last run ({status}); they are retried next run."
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Email digest of strong new matches")
    ap.add_argument("--dry-run", action="store_true",
                    help="write the email to the job summary instead of sending it")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    from agents.grants.agent import store_path

    try:
        settings = load_grants_config(CONFIG_PATH).alerts
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        status = run(dry_run=args.dry_run, env=dict(os.environ), now=datetime.now(UTC),
                     store_path=store_path(), state_file=state_path(), settings=settings,
                     summary_path=Path(summary) if summary else None)
    except Exception as e:  # never fail the job over an email
        log.exception("alerts failed")
        status = f"failed: {e}"
    print(f"alerts: {status}")
    if status.startswith(("skipped", "failed")):
        print(f"::warning::Email alerts {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
