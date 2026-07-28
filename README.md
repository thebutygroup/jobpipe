# jobpipe

**Inverted job search: describe the job you want, and matching jobs find you.**

jobpipe watches 7 job sources around the clock, deduplicates everything it
finds into canonical jobs, reads each posting in full, and scores it 0–10
against what each user said they want — then shows the *why*, not just the
number. Users sign up with a username, one job title and one sentence; matches
start within minutes.

Live at [jobs.thebutygroup.com](https://jobs.thebutygroup.com).

## What's inside

- **7 sources, one interface** — ATS boards (Greenhouse, Lever, Ashby,
  Workable), Built In saved-search scraping, and the Adzuna + Reed.co.uk
  aggregator APIs, all behind a common `SourceAdapter` contract. Adding
  source N+1 = one adapter class + one `register()` call + one config entry.
  Sources without API keys **no-op gracefully** (clear log line,
  "unconfigured" on the dashboard) — nothing breaks while keys are pending.
- **Cross-source identity resolution** — the same job on three sites becomes
  ONE canonical posting with full per-source provenance (`source_postings`).
  Matching heuristic: normalized company + exact-then-fuzzy title
  (rapidfuzz) + location compatibility (incl. UK-postcode handling). ATS
  postings are preferred canonical records; every merge is logged as an
  auditable `dedupe_linked` event.
- **Source analytics** (`/sources`) — which sources earn their keep:
  uniqueness %, pairwise overlap matrix, who-saw-it-first, volume/day, and
  match quality per source, all derived at read time from provenance.
- **Self-serve signup, confirmed by email** — signing up is inert until the
  address is proven: the account is created inactive and gets exactly one
  email, a confirmation link. Nothing is matched, and no model call is spent,
  until it's clicked. Confirming is what activates the account (up to
  `SIGNUP_DAILY_CAP`/day, default 3), fires the instant mini match run
  (`SIGNUP_INSTANT_MATCHES` newest postings, default 20) and sends the branded
  welcome. Beyond the cap: pending + a flag banner + an owner email. The owner
  is told about every signup either way. LLM spend is bounded by
  `MATCH_DAILY_CALL_CAP` regardless.
- **Per-user match pages** — `/job_matches/<name>` (cards with highlights and
  a you-want/job-offers alignment table) and `/all/<name>` (dense sortable
  table). Anonymous by design: no names, no contact details, salary only as a
  relative signal.
- **Design token system** — one `:root` block in `base.html` themes the whole
  site ("Fresh botanical" palette); alternative palettes documented inline.
  One change propagates everywhere; no images, no static-file pipeline.
- **Application pipeline (owner-side)** — prefilter → one Haiku call per
  posting per profile → review queue → human-gated Playwright submitter
  (off by default) → email outcome tracking → funnel analytics (`/stats`).

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
                    ▲
                    │ scored only for applicants that are active,
                    │ email-confirmed and not shadow-banned
   signup → confirmation email → /confirm/<user>/<token> → activate
```

Runtime: three containers (Django dashboard behind gunicorn, APScheduler
worker, dormant Playwright submitter) sharing one image, SQLite on a volume,
reached through a Cloudflare Tunnel. `deploy/gcp/` has a tested VM
lift-and-shift runbook (with Litestream → GCS backups) if/when cloud hosting
appeals.

## Quickstart

1. `cp app/.env.example app/.env` and fill in: `ANTHROPIC_API_KEY`, mail
   settings (Gmail: app password; `MAIL_FROM` for a branded sender),
   `DJANGO_SECRET_KEY`, and — to enable the aggregators —
   `ADZUNA_APP_ID`/`ADZUNA_APP_KEY` (developer.adzuna.com) and
   `REED_API_KEY` (reed.co.uk/developers/jobseeker).
   Windows host: save ASCII, no BOM, no `#`/`$` in values.
2. `cp app/searches.yaml.example app/searches.yaml` — one entry per
   (source, query); location is a per-search parameter (London default).
   Optionally seed `app/companies.yaml` with ATS boards you care about.
3. `docker compose build && docker compose up -d`
   (see `docker-compose.example.yml`; production embeds the services in a
   larger stack behind cloudflared).
4. First data: `docker compose exec jobpipe-scheduler python -m
   jobpipe.pollers.runner`, then `... -m jobpipe.matching.matcher`.
   Thereafter the scheduler polls 06:00/18:00 and matches 06:45.
5. No API keys yet? `python scripts/demo_ingest.py` pushes toy fixtures
   through the full adapter+dedupe path so every dashboard has data.

## Development

- **Tests**: `cd app && pip install -e ".[dev]" && pytest` (~205 tests:
  adapter normalization against fixtures, dedupe nasty cases, migration,
  signup/confirmation/auto-activation, analytics math, dashboard rendering).
  Many are named after the incident that motivated them — when you fix a bug,
  leave a test behind that would have caught it.
  `scripts/record_fixtures.py` verifies live API keys and records real
  responses as fixtures that CI then validates against.
- **Lint**: `ruff check .` — CI (`.github/workflows/ci.yml`) runs ruff +
  pytest on every push.
- **Test accounts**: sign up with the `testrun` username prefix, then
  `docker compose exec jobpipe-web python scripts/cleanup_testruns.py`
  purges them and refunds their activation slots (never real users').
- **Theming**: edit the `:root` tokens in
  `app/jobpipe/dashboard/templates/base.html` — sage/cream and terracotta
  alternates are documented beside the active palette.
- **Ops scripts** (`app/scripts/`): approve_user, rename/export/backfill
  helpers, demo_ingest, record_fixtures, cleanup_testruns.

## Roadmap

**The big one — the application flow.** The end state: you're on your phone,
you review your matches, and you apply in a tap. jobpipe stores your assets —
including different resumes for different position types — prepares the
application, and submits it for you (human-approved, rate-capped). The
foundations exist (profile schema with resume variants, answer preparation,
a gated Playwright submitter, outcome tracking); the work is making it
per-user, mobile-first, and self-serve: asset upload, an apply button on the
match pages with in-place confirmation, and lightweight auth to protect it.

Supporting improvements (from live user testing):

1. **UI look & feel** — continued polish beyond the token-system foundation.
2. **Built In cookie handling** — the scraper currently rides a single
   personal cookie; multiple users need per-session or cookie-less handling.
3. **Structured user testing** — first cohort feedback: what's good, what's bad.
4. **A recurring digest** — there is still no periodic "new since last time"
   email for existing users, only the one-time matches-ready send.

Further out: email↔username linkage (one email = one account), a JSON API for
a native mobile client, recruiter-agency dedupe (Reed), Cloud Run + Cloud SQL
port (`deploy/gcp/RUNBOOK.md` is the stepping stone).

Recently shipped: email-confirmed signup (double opt-in), self-serve profile
editing, and in-place action feedback on the review queue.

## Safety

Submissions are disabled by default (`SUBMIT_ENABLED=false`) and human-gated
per application; compliance answers resolve only from structured profile
data; daily caps bound LLM spend, aggregator API calls, signup activations
and submission volume. Not affiliated with any job board — polling is polite
(robots.txt, cooldowns, per-domain pacing, capped API usage).
