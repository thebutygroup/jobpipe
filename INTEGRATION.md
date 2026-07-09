# Integrating jobpipe into your C:\stack

jobpipe is packaged to match your post-migration stack: separate web / scheduler
/ submitter containers, python:3.13-slim + gunicorn, **no published host ports**,
routed by the shared cloudflared container via service name. It follows your
documented "Adding a NEW app to this stack" pattern exactly.

## What goes where
Copy the `jobpipe\` folder (the one containing `Dockerfile` and `app\`) into
`C:\stack\`, so you end up with `C:\stack\jobpipe\`. Your stack becomes:

    C:\stack\
      docker-compose.yml     <- you'll paste 3 service blocks in here
      .env                   <- untouched (TUNNEL_TOKEN)
      bot\  web\  jobpipe\   <- new

---

## Step 1 — place the files
```powershell
# from wherever you unzipped this:
Move-Item .\jobpipe C:\stack\jobpipe
cd C:\stack\jobpipe
dir   # expect: Dockerfile, .dockerignore, app\
```

## Step 2 — create .env (ASCII, no BOM — your stack's trap)
```powershell
cd C:\stack\jobpipe\app
Set-Content -Path .env -Encoding ascii -Value (Get-Content .env.example)
notepad .env   # fill values. Reminder: no hash or dollar characters inside values.
```
Minimum to start read-only: ANTHROPIC_API_KEY, SMTP_USER, SMTP_PASSWORD
(Zoho app password), NOTIFY_TO. Generate the CSRF key:
```powershell
docker run --rm python:3.13-slim python -c "import secrets;print(secrets.token_hex(32))"
```
Paste it as DJANGO_SECRET_KEY (hex = no forbidden characters). Leave
SUBMIT_ENABLED=false.

## Step 3 — create profile.yaml (YAML; # comments are fine here, unlike .env)
```powershell
Set-Content -Path profile.yaml -Encoding ascii -Value (Get-Content profile.example.yaml)
notepad profile.yaml   # fill identity + preferences (titles are pre-seeded)
```
Drop your resume PDF into `C:\stack\jobpipe\app\assets\` and point
`documents.resume_default` at it (path as seen in-container: assets/yourfile.pdf).

## Step 4 — add the services to the stack compose
Open `C:\stack\docker-compose.yml` and paste the three blocks from
`COMPOSE-SERVICES-TO-ADD.yml` into the `services:` section (mind YAML indent —
two spaces, same level as `web:` and `bot:`). Save as ASCII.

## Step 5 — build + verify registry (nothing exposed yet)
```powershell
cd C:\stack
docker compose build jobpipe-web
docker compose run --rm jobpipe-web python scripts/verify_registry.py
```
Fix or delete any DEAD tokens in `jobpipe\app\companies.yaml` (they're guesses).

## Step 6 — seed once, then start web + scheduler
```powershell
docker compose run --rm jobpipe-web python -m jobpipe.pollers.runner
docker compose run --rm jobpipe-web python -m jobpipe.matching.matcher
docker compose up -d jobpipe-web jobpipe-scheduler
docker compose logs -f jobpipe-scheduler   # watch a cycle; Ctrl-C to stop tailing
```
At this point discovery + matching + dashboard are live *inside* the stack, but
not yet reachable from outside. (Do NOT start jobpipe-submitter yet.)

## Step 7 — expose the dashboard via the tunnel (dashboard-managed, like your others)
In Cloudflare Zero Trust -> Networks -> Tunnels -> home-bot01 -> Published
application routes, add:

    Hostname:  jobs.thebutygroup.com
    Service:   http://jobpipe-web:8010

Then add the DNS CNAME (jobs -> <tunnel-id>.cfargotunnel.com, proxied) if the
dashboard doesn't create it for you. No compose change — cloudflared already
routes by service name on the shared network.

## Step 8 — lock it down (it holds PII: phone, visa, salary)
Cloudflare Zero Trust -> Access -> Applications -> Add:
    Application domain: jobs.thebutygroup.com
    Policy: Allow, your email, One-Time PIN
Confirm an incognito visitor hits the Access wall before trusting it.

## Step 9 — email + reboot checks
```powershell
docker compose run --rm jobpipe-web python -m jobpipe.publish   # sends a test digest now
```
Then reboot the box and confirm Docker Desktop + all containers return on sign-in
(your documented SPOF). The 08:00 email should arrive next morning.

---

## When ready for v1 submitting (after a week or two read-only)
1. Start the submitter (still inert while SUBMIT_ENABLED=false):
   ```powershell
   docker compose up -d jobpipe-submitter
   docker compose logs -f jobpipe-submitter
   ```
   It will prepare applications and queue them; the daily email's links open each
   application's review page. Watch several, confirm answers look right.
2. Only then flip the master switch:
   ```powershell
   # edit jobpipe\app\.env -> SUBMIT_ENABLED=true  (ASCII, no BOM)
   docker compose up -d jobpipe-submitter
   ```

## Stack-specific notes carried over from your infra summary
- No host ports: everything routes as http://jobpipe-web:8010 internally. Never
  add a `ports:` line — it breaks the model and re-exposes to the LAN.
- Deploy after code changes = `docker compose build jobpipe-web && docker compose
  up -d jobpipe-web jobpipe-scheduler` (the submitter installs editable, so it
  picks up changes on restart).
- The submitter mounts the whole app dir (editable install) because Playwright's
  image installs jobpipe at container start; web/scheduler bake it into the image.
- CAPTCHA default is human: the submitter pauses to NEEDS_HUMAN and emails you;
  solve at the machine. (noVNC solve.thebutygroup.com route is a later add.)
- Uptime: worth adding jobs.thebutygroup.com to the UptimeRobot you flagged.
