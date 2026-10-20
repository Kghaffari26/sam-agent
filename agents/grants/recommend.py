"""Contractor setup: turn what a contractor enters on the dashboard (a
`ContractorIntake`) into a complete business profile with recommended
`[filters]` and `[scoring]` (SPEC_GRANTS.md §2.1).

Deterministic and explainable, no LLM call: the intake's description and
services are matched against a catalog of federal trades (NAICS, PSC, keywords,
grant keywords), and the firm's size, certifications, capacity and bid strategy
pick the value range, filters, rubric weights and recommendation bands. Every
choice comes with a one-line reason (`ProfileRecommendation.rationale`) so the
dashboard can show *why* and the contractor can override it.

    uv run python -m tools.setup_contractor intake.json   # see tools/setup_contractor.py
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from agents.grants.config import (
    DEFAULT_WEIGHTS,
    BusinessProfile,
    Clearance,
    EntityType,
    ProfileFilters,
    ScoringPreferences,
)
from agents.grants.setasides import SET_ASIDES

BidStrategy = Literal["selective", "balanced", "volume"]

# Accepted spellings -> the certification names setasides.py checks for.
_CERT_ALIASES: dict[str, str] = {
    "8a": "8A",
    "8(a)": "8A",
    "hubzone": "HUBZone",
    "hub zone": "HUBZone",
    "sdvosb": "SDVOSB",
    "service-disabled veteran-owned": "SDVOSB",
    "vosb": "VOSB",
    "veteran-owned": "VOSB",
    "wosb": "WOSB",
    "women-owned": "WOSB",
    "edwosb": "EDWOSB",
}


class ContractorIntake(BaseModel):
    """What a contractor fills in on the dashboard. Only `company_name` and
    `description` are required; everything else sharpens the recommendation."""

    company_name: str
    description: str  # what the company does, in its own words
    services: list[str] = Field(default_factory=list)  # e.g. ["HVAC repair", "boiler service"]
    entity_type: EntityType = "small_business"
    certifications: list[str] = Field(default_factory=list)  # 8(a), HUBZone, SDVOSB, WOSB...
    sam_registered: bool = True
    naics: list[str] = Field(default_factory=list)  # codes the firm already uses, best first
    employees: int | None = None
    annual_revenue: float | None = None
    typical_contract_min: float | None = None
    typical_contract_max: float | None = None
    states: list[str] = Field(default_factory=list)  # where it can perform (2-letter codes)
    remote_ok: bool | None = None  # None: inferred from the trades
    clearance: Clearance = "none"
    past_performance: list[str] = Field(default_factory=list)
    target_agencies: list[str] = Field(default_factory=list)
    excluded_agencies: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)  # work it never wants
    interested_in_grants: bool | None = None  # None: inferred
    bid_strategy: BidStrategy = "balanced"
    team_capacity_note: str = ""

    @field_validator("certifications")
    @classmethod
    def _normalize_certs(cls, certs: list[str]) -> list[str]:
        known = {"8A", "HUBZone", "SDVOSB", "VOSB", "WOSB", "EDWOSB"}
        out: list[str] = []
        for c in certs:
            name = _CERT_ALIASES.get(c.strip().lower(), c.strip())
            if name not in known:
                raise ValueError(f"unknown certification {c!r}; use one of {sorted(known)}")
            if name not in out:
                out.append(name)
        return out

    @field_validator("states")
    @classmethod
    def _upper_states(cls, states: list[str]) -> list[str]:
        return [s.strip().upper() for s in states if s.strip()]


# ---- the trade catalog ---------------------------------------------------------------

Family = Literal["knowledge", "field", "goods", "care", "logistics"]


@dataclass(frozen=True)
class Trade:
    id: str
    label: str
    family: Family
    cues: tuple[str, ...]  # words in the intake that suggest this trade
    naics_primary: tuple[str, ...]
    naics_secondary: tuple[str, ...]
    psc_prefixes: tuple[str, ...]
    keywords: tuple[str, ...]
    grant_keywords: tuple[str, ...] = ()
    # Words that mark a notice as this trade's work: negative keywords for
    # firms in other families.
    signature: tuple[str, ...] = ()
    remote: bool = False  # work that can usually be done off-site


TRADES: tuple[Trade, ...] = (
    Trade("it_software", "Software and IT services", "knowledge",
          ("software", "web", "application", "app", "cloud", "devops", "data", "it services",
           "programming", "developer", "saas", "database", "ai", "machine learning"),
          ("541511", "541512"), ("541519", "518210"), ("DA", "DB", "DH", "DF"),
          ("software development", "web application", "cloud", "modernization", "data",
           "DevSecOps", "API", "artificial intelligence"),
          ("software", "data", "digital", "artificial intelligence", "SBIR"),
          ("software development",), remote=True),
    Trade("cybersecurity", "Cybersecurity", "knowledge",
          ("cyber", "cybersecurity", "security operations", "penetration", "zero trust", "soc",
           "vulnerability", "rmf", "ato", "fedramp"),
          ("541512",), ("541519", "541690"), ("DJ",),
          ("cybersecurity", "zero trust", "vulnerability", "penetration testing",
           "security operations", "RMF"),
          ("cybersecurity",), (), remote=True),
    Trade("consulting", "Management and program consulting", "knowledge",
          ("consulting", "consultant", "program management", "acquisition", "strategy",
           "change management", "business process", "advisory", "pmo", "project management"),
          ("541611",), ("541618", "541690", "541612"), ("R4", "R7"),
          ("program management", "acquisition support", "consulting", "strategic planning",
           "process improvement", "PMO"),
          ("capacity building", "technical assistance"), (), remote=True),
    Trade("engineering", "Engineering and architecture", "field",
          ("engineering", "engineer", "architect", "architecture", "a-e", "design services",
           "surveying", "civil", "structural", "mechanical engineering"),
          ("541330",), ("541310", "541370", "541380"), ("C1", "R4"),
          ("engineering", "architect-engineer", "design", "A-E services", "surveying",
           "feasibility study"),
          ("infrastructure",), ()),
    Trade("marketing", "Marketing and communications", "knowledge",
          ("marketing", "communications", "outreach", "advertising", "graphic design",
           "video production", "public relations", "branding", "social media"),
          ("541810", "541613"), ("541430", "541820", "512110"), ("R7", "T0"),
          ("outreach", "communications", "marketing", "public awareness", "graphic design",
           "video production"),
          ("outreach", "communications"), (), remote=True),
    Trade("training", "Training and education", "knowledge",
          ("training", "curriculum", "instructional design", "e-learning", "education",
           "workforce development", "coaching"),
          ("611430",), ("611710", "611699"), ("U0",),
          ("training", "curriculum development", "instructional design", "e-learning",
           "workforce development"),
          ("education", "workforce development", "training"), (), remote=True),
    Trade("research", "Research and development", "knowledge",
          ("research", "r&d", "laboratory", "prototype", "sbir", "sttr", "scientific",
           "scientific research"),
          ("541715",), ("541720", "541690"), ("A",),
          ("research", "prototype", "SBIR", "STTR", "evaluation", "study"),
          ("research", "SBIR", "STTR", "innovation"), ()),
    Trade("translation", "Translation and interpretation", "knowledge",
          ("translation", "interpretation", "interpreter", "linguist", "language services"),
          ("541930",), (), ("R6",),
          ("translation", "interpretation", "linguist", "language services"),
          (), (), remote=True),
    Trade("construction", "General construction", "field",
          ("construction", "general contractor", "renovation", "homebuilder",
           "design-build", "remodel", "paving", "roofing", "concrete"),
          ("236220",), ("236210", "237310", "237990", "238160", "238110"), ("Y1", "Z1", "Z2"),
          ("construction", "renovation", "repair", "design-build", "roof", "paving"),
          (), ("construction", "renovation", "paving")),
    Trade("electrical", "Electrical contracting", "field",
          ("electrical", "electrician", "lighting", "generator", "wiring", "solar",
           "power distribution"),
          ("238210",), ("238990", "221114"), ("Z2", "N0", "J0"),
          ("electrical", "lighting", "generator", "wiring", "power distribution", "solar"),
          ("energy efficiency",), ("electrical",)),
    Trade("hvac_plumbing", "HVAC and plumbing", "field",
          ("hvac", "heating", "air conditioning", "plumbing", "boiler", "chiller",
           "mechanical contractor", "refrigeration"),
          ("238220",), ("238990",), ("Z2", "J0", "N0"),
          ("HVAC", "plumbing", "boiler", "chiller", "air conditioning", "mechanical"),
          ("energy efficiency",), ("HVAC", "plumbing")),
    Trade("facilities", "Janitorial, grounds and facilities", "field",
          ("janitorial", "custodial", "cleaning", "facilities maintenance", "landscaping",
           "grounds", "lawn", "pest control", "snow removal", "facility"),
          ("561720", "561210"), ("561730", "561710", "561790"), ("S2",),
          ("janitorial", "custodial", "facilities maintenance", "grounds maintenance",
           "landscaping", "pest control"),
          (), ("janitorial", "custodial", "grounds maintenance", "landscaping")),
    Trade("security_guard", "Security guard services", "field",
          ("security guard", "guard services", "armed guard", "unarmed guard", "patrol",
           "access control officer"),
          ("561612",), ("561621",), ("S206",),
          ("security guard", "guard services", "armed guard", "unarmed guard", "patrol"),
          (), ("guard services", "security guard")),
    Trade("healthcare", "Healthcare and medical staffing", "care",
          ("medical", "nursing", "nurse", "clinical", "healthcare", "health care",
           "telehealth", "physician", "dental", "behavioral health"),
          ("621111", "621399"), ("621999", "561320", "621610"), ("Q",),
          ("medical services", "nursing", "clinical", "telehealth", "behavioral health"),
          ("health", "behavioral health", "public health"), ("nursing", "clinical services")),
    Trade("logistics", "Logistics and transportation", "logistics",
          ("logistics", "freight", "trucking", "moving", "warehousing", "warehouse",
           "transportation", "shipping", "courier", "relocation"),
          ("484110", "488510"), ("484121", "484210", "493110", "492110"), ("V1",),
          ("freight", "logistics", "moving services", "warehousing", "transportation"),
          ("transportation",), ("freight", "moving services", "warehousing")),
    Trade("it_products", "IT hardware and software resale", "goods",
          ("reseller", "hardware", "laptops", "licenses", "var", "value-added reseller",
           "equipment supply"),
          ("423430",), ("541519", "443142"), ("70", "7A", "7B", "7E"),
          ("hardware", "software licenses", "laptops", "servers", "brand name or equal"),
          (), ("brand name or equal",)),
    Trade("supplies", "Products and supplies", "goods",
          ("supplies", "supplier", "distributor", "manufacturing", "manufacturer",
           "wholesale"),
          ("423990",), ("424120", "423840", "339999"), (),
          ("supplies", "equipment", "products", "brand name or equal"),
          (), ("supplies", "fuel", "ammunition", "vehicle")),
)

TRADES_BY_ID = {t.id: t for t in TRADES}
MAX_TRADES = 3
MAX_KEYWORDS = 12
MAX_NEGATIVE_KEYWORDS = 10
MAX_SECONDARY_NAICS = 6
CAPABILITY_FLOOR = 30  # whether the firm can do the work stays the biggest factor

ALL_CONTRACT_NOTICE_TYPES = [
    "solicitation",
    "combined_synopsis_solicitation",
    "presolicitation",
    "sources_sought",
]


def _has(cue: str, text: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(cue.lower()) + r"(?![a-z0-9])", text) is not None


def match_trades(intake: ContractorIntake) -> list[tuple[Trade, int]]:
    """Trades the intake describes, best first: one point per cue found in the
    description/services, plus three when the firm already uses one of the
    trade's NAICS codes; trades scoring under half the best one are dropped.
    NAICS alone only decides when no cue matches anything (codes like 541512 are
    shared by several trades)."""
    text = " ".join([intake.description, *intake.services]).lower()
    by_cue: list[tuple[Trade, int]] = []
    by_naics: list[tuple[Trade, int]] = []
    for trade in TRADES:
        cues = sum(1 for cue in trade.cues if _has(cue, text))
        naics = 3 if set(intake.naics) & set(trade.naics_primary + trade.naics_secondary) else 0
        if cues:
            by_cue.append((trade, cues + naics))
        elif naics:
            by_naics.append((trade, naics))
    scored = by_cue or by_naics
    scored.sort(key=lambda pair: -pair[1])
    # A passing mention ("patient data") doesn't add a second trade.
    return [p for p in scored if p[1] * 2 >= scored[0][1]][:MAX_TRADES] if scored else []


# ---- the recommendation --------------------------------------------------------------


@dataclass
class ProfileRecommendation:
    profile: BusinessProfile
    trades: list[str]  # matched trade labels, best first
    # Dotted profile field -> why it was set this way (shown on the dashboard and
    # written as comments into the TOML).
    rationale: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "profile": self.profile.model_dump(mode="json"),
            "trades": self.trades,
            "rationale": self.rationale,
        }


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "contractor"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(item.strip())
    return out


def _value_range(intake: ContractorIntake) -> tuple[float | None, float | None, str]:
    if intake.typical_contract_min is not None or intake.typical_contract_max is not None:
        return (intake.typical_contract_min, intake.typical_contract_max,
                "From the contract sizes you entered.")
    if intake.annual_revenue:
        rev = intake.annual_revenue
        low = max(10_000.0, round(rev * 0.01, -3))
        high = round(rev * 1.5, -3)
        return (low, high, "1% to 150% of annual revenue: smaller isn't worth a proposal, "
                           "larger is hard to staff and to show past performance for.")
    bands = [(10, 10_000.0, 1_000_000.0), (50, 25_000.0, 5_000_000.0),
             (250, 100_000.0, 25_000_000.0)]
    if intake.employees is not None:
        for top, low, high in bands:
            if intake.employees <= top:
                return (low, high, f"Typical range for a firm of {intake.employees} people.")
        return (250_000.0, None, "Large firm: no upper limit; skips small buys.")
    return (None, None, "No size information: no value limits.")


def _min_days(intake: ContractorIntake) -> tuple[int, str]:
    n = intake.employees
    if n is None:
        return 5, "Default: at least 5 days to respond."
    if n <= 10:
        return 10, "A team of 10 or fewer needs about 10 days to write a full proposal."
    if n <= 50:
        return 7, "A mid-size team needs about a week to respond."
    return 5, "A larger proposal team can turn a response in 5 days."


def _weights(
    intake: ContractorIntake, held_set_aside: bool, rationale: list[str]
) -> dict[str, int]:
    w = dict(DEFAULT_WEIGHTS)

    def shift(src: str, dst: str, pts: int, why: str) -> None:
        floor = CAPABILITY_FLOOR if src == "capability" else 5
        pts = min(pts, w[src] - floor)
        if pts > 0:
            w[src] -= pts
            w[dst] += pts
            rationale.append(f"{dst} +{pts} (from {src}): {why}")

    if not intake.past_performance:
        shift("capability", "eligibility", 5,
              "no federal past performance yet, so restricted competitions count more")
    if held_set_aside:
        shift("strategic", "eligibility", 3,
              "your certifications open set-aside competitions with fewer bidders")
    if intake.employees is not None and intake.employees <= 10:
        shift("capability", "size", 3, "a small team must match the contract size")
        shift("capability", "timeline", 2, "a small team has less slack for tight deadlines")
    if intake.target_agencies:
        shift("capability", "strategic", 3,
              "you named target agencies, so agency fit matters more")
    return w


def recommend_profile(intake: ContractorIntake) -> ProfileRecommendation:
    why: dict[str, str] = {}
    matched = match_trades(intake)
    trades = [t for t, _ in matched]
    text = " ".join([intake.description, *intake.services]).lower()

    # NAICS / PSC
    if intake.naics:
        naics_primary = intake.naics[:3]
        why["naics_primary"] = "The NAICS codes you already use."
    elif trades:
        naics_primary = list(trades[0].naics_primary)
        why["naics_primary"] = f"Core NAICS for {trades[0].label.lower()}."
    else:
        naics_primary = []
        why["naics_primary"] = "No trade recognized: add your NAICS codes."
    secondary = [c for t in trades for c in (*t.naics_primary, *t.naics_secondary)]
    secondary += intake.naics[3:]
    naics_secondary = [c for c in _dedupe(secondary) if c not in naics_primary]
    naics_secondary = naics_secondary[:MAX_SECONDARY_NAICS]
    if naics_secondary:
        why["naics_secondary"] = "Related codes for the trades in your description."
    psc = _dedupe([p for t in trades for p in t.psc_prefixes])
    if psc:
        why["psc_prefixes"] = "Product/service code families federal buyers use for this work."

    # Keywords: the contractor's own words first, then the trade vocabulary,
    # cue-matched entries before the rest.
    services = [s for s in intake.services if len(s.split()) <= 5]
    trade_words = [k for t in trades for k in t.keywords]
    trade_words.sort(key=lambda k: not _has(k, text))
    keywords = _dedupe(services + trade_words)[:MAX_KEYWORDS]
    why["keywords"] = "Your services plus the terms agencies use for this work."

    own = {k.lower() for k in keywords} | {s.lower() for s in intake.services}
    families = {t.family for t in trades}
    negatives = list(intake.avoid)
    if families:
        negatives += [s for t in TRADES if t.family not in families for s in t.signature]
    negatives = [n for n in _dedupe(negatives) if n.lower() not in own
                 and not any(n.lower() in o for o in own)][:MAX_NEGATIVE_KEYWORDS]
    if negatives:
        why["negative_keywords"] = (
            "Your 'avoid' list plus other trades' work, rejected by title before any AI cost."
        )

    # Place
    remote_ok = intake.remote_ok
    if remote_ok is None:
        remote_ok = bool(trades) and all(t.remote for t in trades)
        why["remote_ok"] = ("Your work can usually be done remotely." if remote_ok else
                            "On-site work: notices outside your states are filtered out.")
    states = intake.states if not remote_ok else []
    if intake.states and remote_ok:
        why["states"] = "Remote-capable: no state filter, so out-of-state remote work stays in."

    # Value, deadline, notice types, grants
    min_value, max_value, value_why = _value_range(intake)
    why["min_value"] = why["max_value"] = value_why
    min_days, days_why = _min_days(intake)
    why["min_days_to_respond"] = days_why
    notice_types = list(ALL_CONTRACT_NOTICE_TYPES)
    if intake.bid_strategy == "selective" and intake.past_performance:
        notice_types = ["solicitation", "combined_synopsis_solicitation"]
        why["notice_types"] = "Selective: only notices you can bid on now."
    else:
        why["notice_types"] = ("Includes sources sought and presolicitations: answering "
                               "them gets you known before the RFP drops.")
    include_grants = intake.interested_in_grants
    if include_grants is None:
        include_grants = intake.entity_type in ("nonprofit", "university", "individual") or any(
            t.id == "research" for t in trades
        )
        why["include_grants"] = ("Grants fit your entity type or R&D work." if include_grants
                                 else "Grants rarely fund this kind of firm; contracts only.")
    grant_keywords = _dedupe([k for t in trades for k in t.grant_keywords])[:8]

    # Set-asides held
    eligible_codes = [
        code for code, info in SET_ASIDES.items()
        if (code in ("SBA", "SBP") and intake.entity_type == "small_business")
        or (info.required_certification and info.required_certification in intake.certifications)
    ]
    held_socioeconomic = bool(intake.certifications)

    # [filters]
    filters = ProfileFilters(exclude_sole_source=True)
    why["filters.exclude_sole_source"] = (
        "Sole-source and intent-to-award notices already have a chosen vendor."
    )
    if intake.bid_strategy == "selective":
        families4 = _dedupe([c[:4] for c in naics_primary + naics_secondary])
        if families4:
            filters.naics = families4
            why["filters.naics"] = "Selective: only your NAICS industry groups (4-digit)."
        if intake.entity_type == "small_business" and eligible_codes:
            filters.set_asides = [c for c in eligible_codes if c not in
                                  ("8A", "HZC", "SDVOSBC", "WOSBSS", "EDWOSBSS", "VSS")]
            why["filters.set_asides"] = (
                "Selective small business: only set-asides you qualify for, skipping "
                "full-and-open competitions against large primes."
            )
        filters.negative_keywords_in_description = bool(negatives)
        if negatives:
            why["filters.negative_keywords_in_description"] = (
                "Selective: negative keywords also checked against the description."
            )

    # [scoring]
    weight_notes: list[str] = []
    weights = _weights(intake, held_socioeconomic, weight_notes)
    why["scoring.weights"] = ("; ".join(weight_notes) if weight_notes
                              else "Standard rubric weights.")
    bands = {"selective": (80, 60), "balanced": (None, None), "volume": (70, 50)}
    pursue, consider = bands[intake.bid_strategy]
    why["scoring.pursue"] = {
        "selective": "Selective: Pursue only at fit 80+, Consider at 60+.",
        "balanced": "Balanced: the agent's default bands.",
        "volume": "Volume: Pursue at fit 70+, Consider at 50+, to see more options.",
    }[intake.bid_strategy]
    bonus = 5 if held_socioeconomic else (3 if intake.entity_type == "small_business" else 0)
    if bonus:
        why["scoring.set_aside_bonus"] = (
            f"+{bonus} fit when a notice is set aside for a status you hold (fewer bidders)."
        )
    scoring = ScoringPreferences(weights=weights, pursue=pursue, consider=consider,
                                 set_aside_bonus=bonus)

    summary = " ".join(intake.description.split())
    if intake.employees:
        summary += f" About {intake.employees} employees."
    if intake.states:
        summary += f" Based in / performs in: {', '.join(intake.states)}."
    profile = BusinessProfile(
        id=_slug(intake.company_name),
        name=intake.company_name,
        summary=summary,
        entity_type=intake.entity_type,
        certifications=intake.certifications,
        sam_registered=intake.sam_registered,
        naics_primary=naics_primary,
        naics_secondary=naics_secondary,
        psc_prefixes=psc,
        keywords=keywords,
        negative_keywords=negatives,
        clearance=intake.clearance,
        min_value=min_value,
        max_value=max_value,
        remote_ok=remote_ok,
        states=states,
        target_agencies=intake.target_agencies,
        excluded_agencies=intake.excluded_agencies,
        notice_types=notice_types,
        include_grants=include_grants,
        grant_keywords=grant_keywords,
        min_days_to_respond=min_days,
        team_capacity_note=intake.team_capacity_note,
        past_performance=intake.past_performance,
        filters=filters,
        scoring=scoring,
    )
    return ProfileRecommendation(profile, [t.label for t in trades], why)


# ---- TOML output ---------------------------------------------------------------------


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(int(value)) if float(value).is_integer() else repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"can't write {type(value).__name__} to TOML")


def _toml_lines(data: dict[str, Any], rationale: dict[str, str], prefix: str) -> list[str]:
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, dict) and key not in ("weights",):
            continue  # tables are written after the plain keys
        note = rationale.get(f"{prefix}{key}")
        if note:
            lines.append(f"# {note}")
        if value is None:
            lines.append(f"# {key} = (not set)")
        else:
            lines.append(f"{key} = {_toml_value(value)}")
    return lines


def to_toml(rec: ProfileRecommendation) -> str:
    """The recommended profile as a commented `business_profile.toml`."""
    data = rec.profile.model_dump(mode="json")
    trades = ", ".join(rec.trades) or "none recognized"
    lines = [
        f"# Business profile for {rec.profile.name}, generated by tools/setup_contractor.py",
        f"# from the contractor intake. Matched trades: {trades}.",
        "# Every recommendation below is a starting point: edit freely.",
        "",
        *_toml_lines(data, rec.rationale, ""),
        "",
        "# Filters (SPEC_GRANTS.md §5.2a): SAM.gov's search filters plus extras, applied",
        "# before any AI cost. Empty / unset means no filter.",
        "[filters]",
        *_toml_lines(data["filters"], rec.rationale, "filters."),
        "",
        "# Scoring (SPEC_GRANTS.md §5.4a): weights sum to 100; changing them needs no rescore.",
        "[scoring]",
        *_toml_lines(data["scoring"], rec.rationale, "scoring."),
        "",
    ]
    return "\n".join(lines)
