# GCP migration runbook — home box → Compute Engine VM

Shape: one e2-small VM in europe-west2 (London) running the same Docker stack,
reached through the same Cloudflare Tunnel (no DNS change, no open ports),
SQLite kept on the VM disk with **Litestream** streaming every write to a GCS
bucket for point-in-time restore. Cutover planned for **after the first-user
weekend**; the home box stays as cold standby.

Account: joebuty@thebutygroup.com. Run gcloud from any machine with the
[gcloud CLI](https://cloud.google.com/sdk/docs/install) installed (Mac is easiest).

Cost ballpark: e2-small ~£12–14/mo + 20 GB disk ~£1 + GCS backups pennies.
(e2-micro is free-tier but only in US regions — not worth the latency.)

---

## 1 · One-time GCP setup

```bash
gcloud auth login                       # as joebuty@thebutygroup.com
gcloud projects create jobpipe-prod --name="jobpipe"
gcloud config set project jobpipe-prod
# link billing in console: console.cloud.google.com/billing
gcloud services enable compute.googleapis.com storage.googleapis.com

# backups bucket (name must be globally unique — adjust and mirror it in litestream.yml)
gcloud storage buckets create gs://jobpipe-backups --location=europe-west2 \
    --uniform-bucket-level-access
```

## 2 · The VM

```bash
gcloud compute instances create jobpipe \
    --zone=europe-west2-a --machine-type=e2-small \
    --image-family=debian-12 --image-project=debian-cloud \
    --boot-disk-size=20GB --boot-disk-type=pd-balanced \
    --scopes=storage-rw
# NOTE --scopes=storage-rw lets Litestream write to GCS with the default
# service account — no key files needed.

gcloud compute ssh jobpipe --zone=europe-west2-a
```

On the VM:

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER && exit   # re-ssh so the group applies
```

## 3 · Stack layout on the VM

```bash
gcloud compute ssh jobpipe --zone=europe-west2-a
sudo mkdir -p /opt/jobpipe && sudo chown $USER /opt/jobpipe && cd /opt/jobpipe
git clone https://github.com/thebutygroup/jobpipe.git repo   # main, post-merge
mkdir -p data config
cp repo/deploy/gcp/docker-compose.gcp.yml docker-compose.yml
cp repo/deploy/gcp/litestream.yml litestream.yml   # edit bucket name if changed
```

Copy the private bits from the home box (from your Mac or the home box —
`gcloud compute scp` works from anywhere gcloud is logged in):

```bash
gcloud compute scp C:\stack\jobpipe\app\.env jobpipe:/opt/jobpipe/app.env --zone=europe-west2-a
gcloud compute scp C:\stack\jobpipe\app\profile.yaml jobpipe:/opt/jobpipe/config/ --zone=europe-west2-a
gcloud compute scp C:\stack\jobpipe\app\companies.yaml jobpipe:/opt/jobpipe/config/ --zone=europe-west2-a
gcloud compute scp C:\stack\jobpipe\app\searches.yaml jobpipe:/opt/jobpipe/config/ --zone=europe-west2-a
```

Then append the tunnel token to `app.env` (see §4): `TUNNEL_TOKEN=...`
Also update `DB_PATH` stays `/app/data/jobpipe.db` (unchanged) — the Windows
ASCII/no-BOM rules no longer apply on Linux, but the values are fine as-is.

## 4 · Cloudflare Tunnel

In the Cloudflare Zero Trust dashboard (Networks → Tunnels) create a NEW
tunnel `jobpipe-gcp` (keep the home tunnel alive for rollback), copy its
token into `/opt/jobpipe/app.env` as `TUNNEL_TOKEN=...`, and give it a public
hostname mapping:

    jobs.thebutygroup.com  →  http://jobpipe-web:8010

Do NOT enable the route yet if the home tunnel still owns that hostname —
you'll flip it at cutover (§6). (A hostname can only be served by one tunnel;
the flip is instant and reversible.)

## 5 · First start (shadow mode, before cutover)

```bash
cd /opt/jobpipe && docker compose build jobpipe-web && docker compose up -d
docker compose logs -f --tail=50        # watch it come up
curl -s localhost:8010/healthz          # from the VM: "ok"
```

While the hostname still points at the home box, the GCP stack polls
independently into its own empty DB — fine for a shakedown. Check Litestream
is shipping: `docker compose logs litestream` should show snapshot/WAL uploads,
and the bucket should contain objects.

## 6 · Cutover (Monday, ~15 minutes)

1. **Home box:** `docker compose stop jobpipe-scheduler jobpipe-web` (freezes the DB).
2. Copy the live DB to the VM:
   `gcloud compute scp C:\stack\jobpipe\app\data\jobpipe.db jobpipe:/opt/jobpipe/data/ --zone=europe-west2-a`
   (also copy `jobpipe.db-wal`/`-shm` if present, or run a `PRAGMA wal_checkpoint(TRUNCATE)` first).
3. **VM:** `docker compose restart` — comes up on the real data.
4. **Cloudflare dashboard:** move the `jobs.thebutygroup.com` public hostname
   from the home tunnel to `jobpipe-gcp`.
5. Smoke test: landing page, `/all/joebuty`, `/sources` (health table should
   show all sources ok after the next poll), a test signup end to end.
6. Leave the home stack stopped but intact for a week — that's the rollback.

## 7 · Rollback (any time)

Move the Cloudflare hostname back to the home tunnel and start the home
containers. If GCP took writes you want to keep, copy the DB back first
(reverse of §6.2).

## 8 · Ops on the VM

- Deploy new code: `cd /opt/jobpipe/repo && git pull && cd .. && docker compose build jobpipe-web && docker compose up -d`
  (same push→pull→rebuild rhythm as the home box).
- DB restore drill (do this once so it's boring):
  `docker compose run --rm litestream restore -config /etc/litestream.yml -o /data/restored.db /data/jobpipe.db`
- VM disk snapshots as belt-and-braces: 
  `gcloud compute resource-policies create snapshot-schedule jobpipe-daily --region=europe-west2 --max-retention-days=14 --daily-schedule --start-time=03:00` then attach to the disk.
- Later (post-sprint, if Cloud Run appeals): the deliberate next step is
  porting db.py to Postgres/Cloud SQL; nothing in this VM setup blocks it.
