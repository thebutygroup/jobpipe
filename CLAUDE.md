# CLAUDE.md — jobpipe operations cheat sheet

Working notes for running jobpipe on the home box (Windows, Docker Compose at
`C:\stack\jobpipe`). Also read by Claude sessions working on this repo.

## The stack in one paragraph

Three containers off one image (`jobpipe:latest`): `jobpipe-web` (Django
dashboard, gunicorn, port 8010 → jobs.thebutygroup.com via Cloudflare Tunnel),
`jobpipe-scheduler` (APScheduler: poll 06:00/18:00, match 06:45, prepare
07:15, publish 08:00, track hourly:20, indexscan Sun 05:00 — Europe/London),
`jobpipe-submitter` (Playwright, dormant until SUBMIT_ENABLED=true).
**Code is baked into the image; `.env` and `data/` are mounted.**

## The two kinds of change (get this right and save an hour)

| you changed…            | you must run…                                              |
|-------------------------|------------------------------------------------------------|
| `.env` only             | `docker compose up -d --force-recreate jobpipe-web jobpipe-scheduler` |
| any code / template     | `docker compose build jobpipe-web` **then** the recreate above |

If a change "didn't take", 90% of the time the build step was skipped.
Verify what's actually in the container:
`docker compose exec jobpipe-web grep -c "some new string" /app/jobpipe/somefile.py`

## Applying patches from a Claude session

```powershell
cd C:\stack\jobpipe
git am path\to\the.patch          # browsers may strip hyphens from filenames
# if it fails halfway:
git am --abort                    # clears .git\rebase-apply, then retry
docker compose build jobpipe-web
docker compose up -d --force-recreate jobpipe-web jobpipe-scheduler
```

## Running Python against the live DB (the here-string trick)

PowerShell mangles multiline pastes into `python -c`. Always use a here-string
piped to stdin instead — this is the single most useful pattern in this file:

```powershell
@'
from jobpipe.db import connect
c = connect()
# ...queries...
'@ | docker compose exec -T jobpipe-web python -
```

## Everyday queries

**All users / applicants:**
```powershell
@'
from jobpipe.db import connect
c = connect()
for r in c.execute("SELECT id, name, user_ref, active FROM applicants ORDER BY id"):
    m = c.execute("SELECT COUNT(*) n, COALESCE(SUM(score>=7),0) hits,"
                  " COALESCE(SUM(created_at>=date('now')),0) today"
                  " FROM matches WHERE applicant_id=?", (r["id"],)).fetchone()
    print(f"#{r['id']:<3} {r['name']:<22} /{r['user_ref'] or '-':<16} "
          f"active={r['active']}  scored={m['n']:<5} matches>=7: {m['hits']:<4} today: {m['today']}")
'@ | docker compose exec -T jobpipe-web python -
```

**One user's full profile as the matcher sees it:**
```powershell
@'
from jobpipe.db import connect
c = connect()
r = c.execute("SELECT * FROM applicants WHERE user_ref='joebuty'").fetchone()
print(r["profile_yaml"] or f"file-based: {r['profile_path']}")
'@ | docker compose exec -T jobpipe-web python -
```

**Matches per day (is the matcher alive?):**
```powershell
@'
from jobpipe.db import connect
c = connect()
for r in c.execute("SELECT date(created_at) d, COUNT(*) n,"
                   " COALESCE(SUM(score>=7),0) hits FROM matches"
                   " WHERE created_at>=date('now','-14 days') GROUP BY d ORDER BY d"):
    print(r["d"], f"scored={r['n']}", f"matches={r['hits']}")
'@ | docker compose exec -T jobpipe-web python -
```

**Did signup emails actually send?** (every attempt is recorded)
```powershell
@'
from jobpipe.db import connect
c = connect()
for r in c.execute("SELECT created_at, payload_json FROM events"
                   " WHERE event_type='signup_email' ORDER BY id DESC LIMIT 20"):
    print(r["created_at"], r["payload_json"])
'@ | docker compose exec -T jobpipe-web python -
```

**Postings by board:**
```powershell
@'
from jobpipe.db import connect
c = connect()
for r in c.execute("SELECT source, COUNT(*) n, SUM(duplicate_of IS NULL) canonical"
                   " FROM postings GROUP BY source ORDER BY n DESC"):
    print(f"{r['source']:<10} total={r['n']:<6} canonical={r['canonical']}")
'@ | docker compose exec -T jobpipe-web python -
```

## Monitoring

- **First stop: https://jobs.thebutygroup.com/health** — green means the work
  inside each run succeeded, not just that the run finished. A matcher run
  where every call fails shows DOWN and also emails NOTIFY_TO.
- /sources — per-source polling health, uniqueness, overlap.
- Logs: `docker compose logs --since 24h jobpipe-scheduler | Select-String "ERROR|complete:"`
- Email sends log in the web container:
  `docker compose logs jobpipe-web | Select-String "signup email|email send failed"`

## Caps & knobs (set in app\.env, no rebuild needed)

| env var                        | default | meaning                                  |
|--------------------------------|---------|------------------------------------------|
| MATCH_DAILY_CALL_CAP           | 400     | global model-call ceiling per day        |
| MATCH_DAILY_CALL_CAP_PER_USER  | 50      | per-applicant fairness share (0 = off)   |
| MATCH_THRESHOLD                | 7       | score >= this becomes an application     |
| SIGNUP_DAILY_CAP               | 3       | auto-activated signups/day; beyond → pending |
| SIGNUP_INSTANT_MATCHES         | 20      | postings scored immediately on signup    |

Current values, secrets redacted:
`docker compose exec jobpipe-web python -m jobpipe.config`

## Manual runs

```powershell
docker compose exec jobpipe-scheduler python -m jobpipe.pollers.runner      # poll now
docker compose exec jobpipe-scheduler python -m jobpipe.matching.matcher    # match now
docker compose exec jobpipe-scheduler python -m jobpipe.matching.matcher --applicant 2
docker compose exec jobpipe-web python scripts/cleanup_testruns.py          # purge testrun* users
docker compose exec jobpipe-web python scripts/pick_mvp_posting.py --user joebuty --limit 40
```

The matcher is deliberately sequential (~2s/call): 200 calls ≈ 7–10 min.
Ctrl+C loses nothing — scored postings stay scored.

## Dev (tests run OUTSIDE docker, in app/)

```powershell
cd C:\stack\jobpipe\app
python -m pytest -q
ruff check .
```

## Hard rules

- Assets (resumes) live in `data/assets/` — gitignored AND dockerignored,
  never served by any route, read only by the submitter at submission time.
- `data/builtin_cookies.json` is for Built In DETAIL RESOLUTION ONLY — never
  submission.
- Never commit `.env`, `data/`, or anything under `assets/`.