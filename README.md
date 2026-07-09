# jobpipe

Self-hosted job discovery and application pipeline. Polls ATS boards
(Greenhouse / Lever / Ashby / Workable) and Built In saved searches, resolves
company career sites (weekly index scan + crawler with JSON-LD extraction and
capped LLM fallback), scores every posting against per-user profiles with one
Haiku call each, and serves per-user match pages with highlights, alignment
tables and relative salary signals. Analytics track applications and
email-forwarded outcomes. A scoped Playwright submitter (off by default,
human-review gated) handles submission.

## Layout
- `Dockerfile` — the runtime environment (python:3.13-slim, non-root)
- `app/jobpipe/` — the package: pollers, matching, crawler, indexscan,
  prepare, track, analytics, dashboard (Django), submit
- `app/scripts/` — operational scripts (onboarding backfills, exports,
  apply-flow inspection, manual outcomes)
- `app/tests/` — pytest suite
- `docker-compose.example.yml` — standalone compose (production runs inside
  a larger stack behind a cloudflared tunnel)

## Setup
1. `cp app/.env.example app/.env` and fill in (Anthropic API key, SMTP/IMAP,
   Django secret). Windows: save ASCII, no BOM, no `#`/`$` in values.
2. Create `app/profile.yaml` for the primary applicant (see `app/profile.yaml.example`).
3. `docker compose build && docker compose up -d`
4. First runs: `python -m jobpipe.pollers.runner`, then `python -m
   jobpipe.matching.matcher` (see FIXES-*.md for operational sequences).

Safety: submissions are disabled by default (`SUBMIT_ENABLED=false`), every
application requires human approval, compliance answers resolve only from
structured profile data, and daily caps bound LLM spend and submission volume.

Not affiliated with any job board. Polling is polite (robots.txt, cooldowns,
per-domain pacing).
