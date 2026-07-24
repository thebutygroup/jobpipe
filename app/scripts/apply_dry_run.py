"""MVP dry run: build the Application aggregate, extract the real form,
prepare answers from profile + vault, and show EXACTLY what would be
submitted. Nothing touches the employer beyond a GET of the form page.

  docker compose exec jobpipe-web python scripts/apply_dry_run.py --app-id 42

With --screenshot (run in the submitter container, which has Playwright),
additionally fills the live form with submission DISABLED and saves a
screenshot to data/screenshots/ as visual proof:

  docker compose exec jobpipe-submitter python scripts/apply_dry_run.py \\
      --app-id 42 --screenshot

After a satisfactory dry run the application sits in PENDING_REVIEW with its
answers visible (and editable) on /app/<id> in the dashboard.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jobpipe.apply.models import Application  # noqa: E402
from jobpipe.apply.routes import BROWSER_FORM  # noqa: E402
from jobpipe.db import connect, log_event, now, transition, tx  # noqa: E402
from jobpipe.prepare.answers import resolve  # noqa: E402


def prepare_preview(conn, app: Application) -> dict:
    from jobpipe.apply.platforms.base import PostingClosed
    applier = app.job.applier
    if app.job.route.method != BROWSER_FORM:
        raise SystemExit(
            f"route is {app.job.route.method} on {app.job.route.platform} — "
            "this posting needs manual assist, pick another for the MVP")
    if applier.needs_browser:
        print(f"note: {applier.name} forms are JS-rendered; static extraction "
              "will find nothing — rerun with --screenshot in the submitter "
              "container for browser extraction (Phase 4 completes this).")
    try:
        fields = applier.extract(app.job.route.final_url)
    except PostingClosed as e:
        with tx(conn):
            conn.execute("UPDATE postings SET closed_at = datetime('now') "
                         "WHERE id = ?", (app.job.posting_id,))
            log_event(conn, "apply:posting_closed", application_id=app.id,
                      payload={"detail": str(e)})
        raise SystemExit(f"POSTING CLOSED: {e}\n"
                         "Marked closed in the DB. Re-run pick_mvp_posting.py "
                         "(consider --limit 40) for a live target.")
    if not fields:
        raise SystemExit("no fields extracted (JS form or unexpected markup) — "
                         "browser extraction required")
    answers = {}
    for f in fields:
        r = resolve(f, app.applicant.profile)
        if f.kind == "file":
            resume = app.resume_path
            r.value = str(resume) if resume else ""
            r.source = "vault" if resume else "unknown"
        answers[f.key] = {"label": f.label, "kind": f.kind, "required": f.required,
                          "options": f.options, "value": r.value, "source": r.source,
                          "llm": r.llm, "unknown": r.unknown}
    return answers


def print_preview(app: Application, answers: dict) -> None:
    print(f"\nDRY RUN — application {app.id}")
    print(f"  {app.job.company} — {app.job.title}")
    print(f"  platform: {app.job.route.platform}  form: {app.job.route.final_url}")
    resume = app.resume_path
    print(f"  resume: {resume.name if resume else 'NONE IN VAULT — add one!'}\n")
    for key, a in answers.items():
        flag = ("MISSING" if a["unknown"] else
                "LLM-DRAFT" if a["llm"] else a["source"])
        req = "*" if a["required"] else " "
        print(f"  {req} {a['label'][:44]:44} [{flag:9}] {str(a['value'])[:60]}")
    missing = [a["label"] for a in answers.values() if a["required"] and a["unknown"]]
    print(f"\n  required-but-missing: {missing or 'none'}")


def persist(conn, app: Application, answers: dict) -> None:
    with tx(conn):
        conn.execute("UPDATE applications SET answers_json = ?, resume_variant = ?,"
                     " updated_at = ? WHERE id = ?",
                     (json.dumps(answers),
                      str(app.resume_path.name) if app.resume_path else "",
                      now(), app.id))
        log_event(conn, "apply:dry_run", application_id=app.id,
                  payload={"platform": app.job.route.platform,
                           "fields": len(answers),
                           "missing": sum(1 for a in answers.values()
                                          if a["required"] and a["unknown"])})
    if app.state == "MATCHED":
        transition(conn, app.id, "PREPARED", payload={"via": "apply_dry_run"})
        transition(conn, app.id, "PENDING_REVIEW")
        print("  state: MATCHED -> PENDING_REVIEW (review at /app/%d)" % app.id)


def screenshot(app: Application, answers: dict) -> None:
    """Fill the live form with submit DISABLED; save visual proof."""
    from playwright.sync_api import sync_playwright

    from jobpipe.submit import browser as b

    out = pathlib.Path("data/screenshots")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"dryrun-app{app.id}.png"
    with sync_playwright() as p:
        pw = p.chromium.launch()
        page = pw.new_page(viewport={"width": 1280, "height": 1600})
        page.goto(app.job.route.final_url, wait_until="domcontentloaded")
        blocker = b.detect_blocker(page)
        if blocker:
            print(f"  BLOCKED before filling: {blocker} — this route needs "
                  "manual assist; recorded.")
            page.screenshot(path=str(path), full_page=True)
            pw.close()
            return
        filled = sum(bool(b.fill_field(page, k, meta))
                     for k, meta in answers.items() if meta["value"])
        page.screenshot(path=str(path), full_page=True)
        pw.close()
    print(f"  filled {filled} fields (submit NOT clicked) — proof: {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-id", type=int, required=True)
    ap.add_argument("--screenshot", action="store_true",
                    help="fill the live form (submit disabled) and screenshot; "
                         "requires the Playwright container")
    args = ap.parse_args()
    conn = connect()
    try:
        app = Application.load(conn, args.app_id)
        answers = prepare_preview(conn, app)
        print_preview(app, answers)
        persist(conn, app, answers)
        if args.screenshot:
            screenshot(app, answers)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
