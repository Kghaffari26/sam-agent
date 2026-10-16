# Bid research audit, 2026-10

Scope S3. `scripts/audit_research.py` re-fetched every claim in the five live bid
research briefs from the sources they cite, on GitHub Actions ("Audit - Research",
[run 2](https://github.com/Kghaffari26/sam-agent/actions/runs/38973852083), commit
`7323bb9`; USAspending.gov and sam.gov aren't reachable from the dev container). It
then re-researched the same five opportunities with the fixed code and audited
those briefs the same way. The full report is that run's job summary and its
`research-audit` artifact. The tables below are copied from it.

## Summary

| | Live briefs (as published) | Re-researched with the fixes |
|---|---|---|
| Award ids exist on USAspending | 3/3 ✓ | 14/14 ✓ |
| Award amounts match the brief | 3/3 ✓ (text, to its rounding) | 14/14 ✓ (exact, `prior_awards`) |
| Award agency / NAICS match the notice | 3/3 ✓ / 3/3 ✓ | 14/14 ✓ / 14/14 ✓ |
| Other dollar figures found in a source | 4/4 ✓ | 10/10 ✓ |
| Cited attachments (resourceLinks index, file type) | 7/7 ✓ | 6/6 ✓ |
| Likely incumbent among the cited recipients | none named | 2/2 ✓ |
| Citations only to fetched sources | ✓ | ✓ |
| "No prior awards found" | **4 ✗**: awards exist | no such claim |

Every concrete claim in the live briefs matched its source. What was wrong was what
the briefs left out:

1. **Sub-agency sent as a USAspending toptier agency** (DHA, NOAA, Navy/MCCE, FEMA).
   The tool sent e.g. `{"tier": "toptier", "name": "Department of the Navy"}`.
   USAspending matches names within a tier, so it returned no awards, and four briefs
   said no prior awards exist. With the sub-agency as a subtier, the same NAICS code
   returns 8 awards each (largest: Leidos for DHA, L3Harris for NOAA, Resource
   Management Concepts for the Navy, IBM for FEMA). Fixed in
   `usaspending.awarding_agency_filter` (+ `agency_name` drops a trailing
   "(DHA)"); regression tests from the live trace (`test_sub_agency_is_a_subtier_filter`,
   `test_the_loop_sends_a_sub_agency_as_a_subtier_filter`).
2. **Keywords that match nothing.** After fix 1 the MCCE and FEMA re-research still
   found nothing: the model's keywords narrowed the search to zero. An empty search
   now falls back to the notice's agency and NAICS code without keywords, and says so
   in the tool output (`test_an_empty_search_falls_back_to_the_notice_agency_without_keywords`).
3. **A keywords string failed validation** (`"keywords": "IT operational support"`
   in the live trace), costing the loop a step. `AwardQuery` now accepts one string
   (`test_keywords_sent_as_one_string_are_accepted`).
4. **Leaked tool-call markup** (VA IVX/Cerner, researched 2026-10-14). The finish
   call put `</incumbent_notes>\n<parameter name="prior_award_ids">[...]` inside
   `incumbent_notes`. The brief showed that markup, and its `prior_awards` was empty
   although the three awards it names are real and correctly quoted. `BriefResult`
   now moves leaked parameters back to their fields, and the guard rejects any markup
   left in the text. Regression test from the published response
   (`tests/fixtures/grants/research_finish_leak_live.json`).

Research prompt v1 → v2 (the tool and its description changed), so the daily run
re-researches all five briefs over its next two runs (at most 3 a run). Research eval
on v2: pass rate 1.00, every check 1.00, judge 0.95 (`evals/history.jsonl`).

**`required_tools` 0.90** (the 2026-10-16 research eval) was an expectation issue, not
a model miss. r3 (a Grants.gov grant) went straight to `grants_gov_detail`, which
returns the same facts as `get_opportunity`. Commit `4825be4` made `get_opportunity`
optional for grants cases. Replaying the 2026-10-16 trajectories scores 1.00 on all
five cases, and the live v2 run scored 1.00.

**Attachment names.** The agent names a notice's attachments `attachment_N` in SAM's
`resourceLinks` order. The audit checked that each cited URL is that entry of the
notice's stored `resourceLinks` and that the downloaded file (the daily run's cache) is
the cited type. Fetching SAM's file listing for the real file names costs one SAM.gov
request per notice. None were left today, so it was skipped (`--sam-request-budget`,
default 0).

**SAM usage:** 0 SAM.gov requests by the audit. The ledger for 2026-10-16 stands at 8
of the daily 8 (all from the agent's own runs), within the key's ~10/day. LLM spend
for the two re-research passes: $0.2066 + $0.2280.

## Live briefs (as published)

### 1. DHA EIDS Data Exchange Services (`sam:f680b73a6c10415a83f09f448894e5bb`): 5 checks, 1 ✗

| claim | source | match | detail |
|---|---|---|---|
| cites the notice (SAM.gov) | https://sam.gov/opp/f680b73a6c10415a83f09f448894e5bb/view | ✓ |  |
| cites attachment_1 (pdf) | …/files/9f589a47263e4181bef366f299a0a487/download | ✓ | index matches resourceLinks; file type pdf |
| cites attachment_2 (pdf) | …/files/a690674f6119421b90b0c3925f456189/download | ✓ | index matches resourceLinks; file type pdf |
| likely incumbent: none named | - | – |  |
| no prior awards found | USAspending, agency filter fixed | ✗ | subtier 'Defense Health Agency', NAICS 541512: 8 awards (largest HT003823F0011 LEIDOS, INC.) |

### 2. VA IVX & Cerner to Interface with EHRM (`sam:75b1e510b71d42eb9d1b80524be6c985`): 15 checks, all ✓

| claim | source | match | detail |
|---|---|---|---|
| cites the notice (SAM.gov) | https://sam.gov/opp/75b1e510b71d42eb9d1b80524be6c985/view | ✓ |  |
| cites attachment_1 (pdf) | …/files/c28deedd3719492ba5c3785d61523b4b/download | ✓ | index matches resourceLinks; file type pdf |
| award 36C10B20F0250 exists (text) | USAspending search by award id | ✓ | CACI NSS, LLC, $244,582,347.70 |
| award 36C10B20F0250 agency | USAspending award | ✓ | Department of Veterans Affairs |
| award 36C10B20F0250 NAICS | USAspending award | ✓ | 541519 vs notice 541519 |
| award 36C26020F0744 exists (text) | USAspending search by award id | ✓ | FOUR POINTS TECHNOLOGY, L.L.C., $7,638,488.07 |
| award 36C26020F0744 agency | USAspending award | ✓ | Department of Veterans Affairs |
| award 36C26020F0744 NAICS | USAspending award | ✓ | 541519 vs notice 541519 |
| award 36C26020F0747 exists (text) | USAspending search by award id | ✓ | FOUR POINTS TECHNOLOGY, L.L.C., $1,694,908.70 |
| award 36C26020F0747 agency | USAspending award | ✓ | Department of Veterans Affairs |
| award 36C26020F0747 NAICS | USAspending award | ✓ | 541519 vs notice 541519 |
| figure $244.6M | USAspending award 36C10B20F0250 | ✓ |  |
| figure $7.6M | USAspending award 36C26020F0744 | ✓ |  |
| figure $1.7M | USAspending award 36C26020F0747 | ✓ |  |
| likely incumbent: none named | - | – |  |

All checks pass, but the brief had the leaked markup and an empty `prior_awards` (finding 4).

### 3. NOAAPort Replacement Capabilities (`sam:b2dc823e91cf445db344df6047eeb647`): 6 checks, 1 ✗

| claim | source | match | detail |
|---|---|---|---|
| cites the notice (SAM.gov) | https://sam.gov/opp/b2dc823e91cf445db344df6047eeb647/view | ✓ |  |
| cites attachment_1 (pdf) | …/files/977ec154c83f41da87f1fbe391ac9478/download | ✓ | index matches resourceLinks; file type pdf |
| cites attachment_2 (pdf) | …/files/b288507663ef4c55b2101ee1c96b0f5a/download | ✓ | index matches resourceLinks; file type pdf |
| figure $5M | business profile | ✓ |  |
| likely incumbent: none named | - | – |  |
| no prior awards found | USAspending, agency filter fixed | ✗ | subtier 'National Oceanic and Atmospheric Administration', NAICS 541512: 8 awards (largest DOCDG133E09CN0094 L3HARRIS TECHNOLOGIES, INC.) |

### 4. Marine Corps MCCE OSS and Enterprise AI Integration (`sam:6ab1054610d74407aa19a1316e8677b8`): 4 checks, 1 ✗

| claim | source | match | detail |
|---|---|---|---|
| cites the notice (SAM.gov) | https://sam.gov/opp/6ab1054610d74407aa19a1316e8677b8/view | ✓ |  |
| cites attachment_1 (docx) | …/files/1739ee89c7e941079000a3869d5e0f0c/download | ✓ | index matches resourceLinks; file type docx |
| likely incumbent: none named | - | – |  |
| no prior awards found | USAspending, agency filter fixed | ✗ | subtier 'Department of the Navy', NAICS 541513: 8 awards (largest N0042120F0692 RESOURCE MANAGEMENT CONCEPTS, INC.) |

### 5. FEMA Deployment Management Solution (`sam:cd7c8fcc972c448ebc58596c3f9f5775`): 7 checks, 1 ✗

| claim | source | match | detail |
|---|---|---|---|
| cites the notice (SAM.gov) | https://sam.gov/opp/cd7c8fcc972c448ebc58596c3f9f5775/view | ✓ |  |
| cites attachment_2 (pdf) | …/files/477a8bf78c144a119d10246ddfa9f3c4/download | ✓ | index matches resourceLinks; file type pdf |
| figure $50M | notice / cited attachment text | ✓ |  |
| figure $100M | notice / cited attachment text | ✓ |  |
| figure $5M | business profile | ✓ |  |
| likely incumbent: none named | - | – |  |
| no prior awards found | USAspending, agency filter fixed | ✗ | subtier 'Federal Emergency Management Agency', NAICS 541511: 8 awards (largest HSFEHQ08J2009 INTERNATIONAL BUSINESS MACHINES CORPORATION) |

## Re-researched with the fixes (not published; the daily run republishes)

### 1. DHA: 19 checks, all ✓

| claim | source | match | detail |
|---|---|---|---|
| cites the notice, attachment_1 (pdf), attachment_2 (pdf) | SAM.gov | ✓ | index matches resourceLinks; file type pdf |
| cites prior awards HT003823F0011, HT003818F0004, HT003822F0031 | USAspending.gov | ✓ | each a prior award in the brief |
| award HT003823F0011 exists / amount / agency / NAICS | USAspending | ✓ ✓ ✓ ✓ | LEIDOS, INC., $572,997,098.22; DoD / Defense Health Agency; 541512 |
| award HT003818F0004 exists / amount / agency / NAICS | USAspending | ✓ ✓ ✓ ✓ | LEIDOS, INC., $474,899,181.94; DoD / Defense Health Agency; 541512 |
| award HT003822F0031 exists / amount / agency / NAICS | USAspending | ✓ ✓ ✓ ✓ | LEIDOS, INC., $359,148,583.79; DoD / Defense Health Agency; 541512 |
| likely incumbent: none named | - | – |  |

### 2. VA IVX & Cerner: 18 checks, all ✓

| claim | source | match | detail |
|---|---|---|---|
| cites the notice, attachment_1 (pdf) | SAM.gov | ✓ | index matches resourceLinks; file type pdf |
| cites prior awards VA11817F1888, 36C10B23F0172, 36C10B22F0207 | USAspending.gov | ✓ | each a prior award in the brief |
| award VA11817F1888 exists / amount / agency / NAICS | USAspending | ✓ ✓ ✓ ✓ | DELL FEDERAL SYSTEMS L.P, $1,730,532,626.58; VA; 541519 |
| award 36C10B23F0172 exists / amount / agency / NAICS | USAspending | ✓ ✓ ✓ ✓ | V3GATE, LLC, $576,889,048.72; VA; 541519 |
| award 36C10B22F0207 exists / amount / agency / NAICS | USAspending | ✓ ✓ ✓ ✓ | FOUR POINTS TECHNOLOGY, L.L.C., $521,824,443.39; VA; 541519 |
| likely incumbent: none named | - | – |  |

### 3. NOAAPort: 22 checks, all ✓

| claim | source | match | detail |
|---|---|---|---|
| cites the notice, attachment_1 (pdf), attachment_2 (pdf) | SAM.gov | ✓ | index matches resourceLinks; file type pdf |
| cites prior awards DOCDG133E09CN0094, 1305M420FNWWA0037, 1305M424F0236 | USAspending.gov | ✓ | each a prior award in the brief |
| award DOCDG133E09CN0094 exists / amount / agency / NAICS | USAspending | ✓ ✓ ✓ ✓ | L3HARRIS TECHNOLOGIES, INC., $1,826,088,581.86; Commerce / NOAA; 541512 |
| award 1305M420FNWWA0037 exists / amount / agency / NAICS | USAspending | ✓ ✓ ✓ ✓ | GENERAL DYNAMICS INFORMATION TECHNOLOGY, INC., $125,218,071.95; Commerce / NOAA; 541512 |
| award 1305M424F0236 exists / amount / agency / NAICS | USAspending | ✓ ✓ ✓ ✓ | GENERAL DYNAMICS INFORMATION TECHNOLOGY, INC., $111,626,905.24; Commerce / NOAA; 541512 |
| figures $1.83B, $125M, $112M | USAspending awards | ✓ ✓ ✓ |  |
| likely incumbent 'L3HARRIS TECHNOLOGIES, INC.' | cited awards' recipients | ✓ |  |

### 4. Marine Corps MCCE: 13 checks, all ✓

| claim | source | match | detail |
|---|---|---|---|
| cites the notice, attachment_1 (docx) | SAM.gov | ✓ | index matches resourceLinks; file type docx |
| cites prior awards N0042120F0692, N0042124F0023 | USAspending.gov | ✓ | each a prior award in the brief |
| award N0042120F0692 exists / amount / agency / NAICS | USAspending | ✓ ✓ ✓ ✓ | RESOURCE MANAGEMENT CONCEPTS, INC., $79,282,185.89; DoD / Navy; 541513 |
| award N0042124F0023 exists / amount / agency / NAICS | USAspending | ✓ ✓ ✓ ✓ | RESOURCE MANAGEMENT CONCEPTS, INC., $48,535,910.12; DoD / Navy; 541513 |
| likely incumbent: none named | - | – |  |

### 5. FEMA: 24 checks, all ✓

| claim | source | match | detail |
|---|---|---|---|
| cites the notice | SAM.gov | ✓ |  |
| cites prior awards 70FA3020F00000392, 70FA3019F00000359, HSFEHQ08J2009 | USAspending.gov | ✓ | each a prior award in the brief |
| award 70FA3020F00000392 exists / amount / agency / NAICS | USAspending | ✓ ✓ ✓ ✓ | KARSUN SOLUTIONS LLC, $62,434,187.48; DHS / FEMA; 541511 |
| award 70FA3019F00000359 exists / amount / agency / NAICS | USAspending | ✓ ✓ ✓ ✓ | KARSUN SOLUTIONS LLC, $27,230,757.73; DHS / FEMA; 541511 |
| award HSFEHQ08J2009 exists / amount / agency / NAICS | USAspending | ✓ ✓ ✓ ✓ | INTERNATIONAL BUSINESS MACHINES CORPORATION, $580,425,327.83; DHS / FEMA; 541511 |
| figures $50M, $100M | notice / cited attachment text | ✓ ✓ |  |
| figures $62.4M, $27.2M, $580M | USAspending awards | ✓ ✓ ✓ |  |
| figures $5M, $25K | business profile | ✓ ✓ |  |
| likely incumbent 'KARSUN SOLUTIONS LLC' | cited awards' recipients | ✓ |  |
