"""Email each user their matching profile: what the matcher knows, what's
missing, per-field tips, and their private edit link.

  docker compose exec jobpipe-web python scripts/send_profile_email.py --all
  ... --user eeezee | --dry-run

Skips shadow-banned users and anyone without an email. Recorded as
signup_email events (kind='profile') so sends are auditable.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jobpipe.config import settings  # noqa: E402
from jobpipe.db import connect, log_event, tx  # noqa: E402
from jobpipe.profile_edit import ensure_edit_token, fields_from_row  # noqa: E402

TIPS = {
    "target_titles": "3-5 titles cover the different names employers use for "
                     "the same job. One very specific title is respected too.",
    "positioning": "One or two sentences on what you want and what matters — "
                   "this text is weighed on every single job.",
    "experience": "Years, roles, and 2-3 standout achievements. The single "
                  "biggest upgrade for scoring accuracy.",
    "skills": "The keywords a hiring manager would scan for.",
    "hard_nos": "Anything that makes a job an instant no — saves you reading "
                "matches you'd never take.",
    "salary_min": "Used only to flag jobs above/below your bar. Never shown.",
}


def compose(row, fields: dict, edit_url: str) -> tuple[str, str, str]:
    missing = [k for k in ("experience", "skills", "hard_nos", "salary_min",
                           "positioning") if not (fields.get(k) or "").strip()]
    lines_html, lines_text = [], []
    labels = {"email": "Email", "target_titles": "Titles you want",
              "title_synonyms": "Title variants", "positioning": "What you want",
              "experience": "Experience", "skills": "Skills",
              "locations_ok": "Locations", "hard_nos": "Deal-breakers",
              "salary_min": "Min salary (private)"}
    for k, label in labels.items():
        v = (fields.get(k) or "").strip() or "—"
        lines_html.append(f"<tr><td style='color:#5B6B60;padding:2px 12px 2px 0'>"
                          f"{label}</td><td>{v}</td></tr>")
        lines_text.append(f"  {label}: {v}")
    tips_html = "".join(
        f"<li><b>{labels.get(k, k)}</b>: {TIPS[k]}</li>"
        for k in missing if k in TIPS)
    n_missing = len([k for k in missing if k in TIPS])
    subject = ("Your jobpipe matching profile"
               + (f" — {n_missing} quick upgrades available" if n_missing else ""))
    html = (
        f"<p>This is everything the matcher currently knows about you:</p>"
        f"<table>{''.join(lines_html)}</table>"
        + (f"<p><b>Make your matches sharper</b> — the empty fields that "
           f"matter most:</p><ul>{tips_html}</ul>" if tips_html else
           "<p>Nicely complete — the matcher has plenty to work with.</p>")
        + f"<p><a href='{edit_url}'>Edit your profile here</a> — plain-text "
          f"boxes, saves apply from the next matching run. Keep the link "
          f"private: it's your personal edit key.</p>")
    text = ("Your matching profile:\n" + "\n".join(lines_text)
            + f"\n\nEdit it here (keep this link private):\n{edit_url}\n")
    return subject, html, text


def main() -> None:
    ap = argparse.ArgumentParser()
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--user")
    who.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    conn = connect()
    try:
        if args.user:
            rows = conn.execute("SELECT * FROM applicants WHERE user_ref = ?",
                                (args.user,)).fetchall()
            if not rows:
                raise SystemExit(f"no applicant with user_ref {args.user!r}")
        else:
            rows = conn.execute(
                "SELECT * FROM applicants WHERE active = 1"
                " AND COALESCE(shadow_banned, 0) = 0 ORDER BY id").fetchall()
        base = (settings.dashboard_base_url or "").rstrip("/")
        for row in rows:
            if not row["profile_yaml"]:
                print(f"{row['user_ref']}: file-based profile (owner) — skipped")
                continue
            fields = fields_from_row(row)
            email = (fields.get("email") or "").strip()
            if not email:
                print(f"{row['user_ref']}: no email on profile — skipped")
                continue
            token = ensure_edit_token(conn, row["id"])
            edit_url = f"{base}/profile/{row['user_ref']}/{token}"
            subject, html, text = compose(row, fields, edit_url)
            if args.dry_run:
                print(f"--- {row['user_ref']} -> {email}\nSubject: {subject}\n{text}")
                continue
            from jobpipe import notify
            ok = notify.send_email(subject=subject, html_body=html,
                                   text_body=text, to=email)
            with tx(conn):
                log_event(conn, "signup_email", payload={
                    "kind": "profile", "user_ref": row["user_ref"],
                    "ok": ok, "to": email})
            print(f"{row['user_ref']}: {'sent to ' + email if ok else 'SEND FAILED'}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
