# jobpipe

Self-hosted job discovery and application pipeline — **inverted job search:
describe the job you want, and matching jobs find you.**

Ingests postings from **7 sources behind one adapter interface** — ATS boards
(Greenhouse / Lever / Ashby / Workable), Built In saved searches, and the
Adzuna and Reed.co.uk aggregator APIs — resolves company career sites (weekly
index scan + crawler with JSON-LD extraction and capped LLM fallback),
**deduplicates cross-source postings into canonical jobs with full
provenance**, scores every posting against per-user profiles with one Haiku
call each, and serves per-user match pages with highlights, alignment tables
and relative salary signals. Analytics track applications, email-forwarded
outcomes, and **per-source value** (uniqueness %, overlap matrix, freshness,
match quality — the `/sources` dashboard answers "which sources are worth
having?"). A scoped Playwright submitter (off by default, human-review gated)
handles submission.

## Architecture

```
searches.yaml / companies registry
        │
        ▼
┌─ source adapters (jobpipe/sources) ──────────────────────────┐
│ greenhouse · lever · ashby · workable   (ATS boards)         │
│ builtin                                 (saved-search scrape)│
│ adzuna · reed                           (aggregator APIs)    │
└──────────────┬───────────────────────────────────────────────┘
               ▼  PostingDTO (unified schema)
   dedupe.find_canonical  ── company + title (exact→fuzzy) + location
               ▼
   postings (canonical jobs) ←── source_postings (per-source provenance)
               ▼
   prefilter → Haiku matcher → per-user match pages → review → submit
```

Adding source N+1 = one adapter class + one `register()` call + one
`searches.yaml` entry. Aggregators without API keys **no-op gracefully** (one
log line, `unconfigured` on `/sources`) — nothing breaks while keys are pending.

## Layout
- `Dockerfile` — the runtime environment (python:3.13-slim, non-root)
- `app/jobpipe/` — the package: sources (adapter registry), pollers, dedupe,
  matching, crawler, indexscan, prepare, track, analytics, source_analytics,
  dashboard (Django), submit
- `app/scripts/` — operational scripts (onboarding backfills, exports,
  apply-flow inspection, manual outcomes, fixture demo ingest)
- `app/tests/` — pytest suite (fixture payloads for every adapter; toy data
  until live responses are recorded)
- `.github/workflows/ci.yml` — ruff + pytest on every push
- `docker-compose.example.yml` — standalone compose (production runs inside
  a larger stack behind a cloudflared tunnel)

## Setup
1. `cp app/.env.example app/.env` and fill in (Anthropic API key, SMTP/IMAP,
   Django secret; `ADZUNA_APP_ID`/`ADZUNA_APP_KEY`/`REED_API_KEY` to enable the
   aggregators). Windows: save ASCII, no BOM, no `#`/`$` in values.
2. Create `app/profile.yaml` for the primary applicant (see `app/profile.yaml.example`).
3. Copy `app/searches.yaml.example` → `app/searches.yaml`; each entry sets its
   `source`, keywords and location (London by default — a parameter, not a hardcode).
4. `docker compose build && docker compose up -d`
5. First runs: `python -m jobpipe.pollers.runner`, then `python -m
   jobpipe.matching.matcher` (see FIXES-*.md for operational sequences).
6. Keys in hand? `python scripts/record_fixtures.py` verifies them with one
   live call each and records real responses into `tests/fixtures/*_live.json`
   (commit them — CI then validates against real payload shapes).
   No API keys yet? `python scripts/demo_ingest.py` loads the toy fixture
   payloads through the full adapter + dedupe path so `/sources` and the
   dashboards have data to show.

Safety: submissions are disabled by default (`SUBMIT_ENABLED=false`), every
application requires human approval, compliance answers resolve only from
structured profile data, and daily caps bound LLM spend, aggregator API calls
and submission volume.

Not affiliated with any job board. Polling is polite (robots.txt, cooldowns,
per-domain pacing, capped API usage).
