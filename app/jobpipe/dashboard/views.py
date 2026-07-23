"""Dashboard views.

v1: read views remain GET-only; exactly four write actions exist —
save answers, approve, reject, resume-from-NEEDS_HUMAN — all POST with CSRF.
Approve is blocked while any required field is UNKNOWN/empty. Authentication
stays at the Cloudflare Access edge.
"""

from __future__ import annotations

import json

from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from ..db import IllegalTransition, connect, now, transition, tx
from ..models import APPROVED, PENDING_REVIEW, REJECTED_HUMAN, SUBMITTING


# Fields hidden on the PUBLIC /job_matches pages (name/email/phone/history are fine
# to show per requirements; salary and immigration/visa are not).
SENSITIVE_PUBLIC = ("salary", "compensation", "sponsor", "visa", "immigration",
                    "right to work", "work authori")


def _posting_links(app: dict) -> dict:
    """Links for the detail summary: the listing (apply_url — company/ATS when
    resolvable, else the job-board page) and the company page when known."""
    raw = json.loads(app.get("raw_json") or "{}")
    listing = (app.get("canonical_apply_url") or app.get("apply_url")
               or raw.get("builtin_job_url") or "")
    return {
        "listing": listing,
        "listing_is_board": "builtin" in listing,
        "board_listing": raw.get("builtin_job_url") or "",
        "company_page": raw.get("builtin_company_url") or "",
    }


def _redact_public(fields: list[dict]) -> list[dict]:
    """Drop sensitive fields entirely from a public field list."""
    out = []
    for f in fields:
        label = (f.get("label") or f.get("key") or "").lower()
        if any(term in label for term in SENSITIVE_PUBLIC):
            continue
        out.append(f)
    return out


def _rows(query: str, params: tuple = ()) -> list[dict]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


SORTS = {
    "score": "m.score DESC, a.created_at DESC",
    "title": "p.title COLLATE NOCASE ASC",
    "company": "company COLLATE NOCASE ASC, m.score DESC",
    "newest": "a.created_at DESC",
}


def _list_params(request) -> tuple[str, str, str]:
    """(keyword, sort_key, order_by_sql) from ?q= and ?sort=, whitelisted."""
    q = (request.GET.get("q") or "").strip()[:80]
    sort = request.GET.get("sort", "score")
    if sort not in SORTS:
        sort = "score"
    return q, sort, SORTS[sort]


APP_QUERY = (
    "SELECT a.*, c.name AS company, p.title, p.location, p.apply_url, p.canonical_apply_url,"
    "       p.description_text, p.raw_json, m.score, m.reasons_json, "
    "       m.highlights_json, m.alignment_json "
    "FROM applications a "
    "JOIN postings p ON p.id = a.posting_id "
    "JOIN companies c ON c.id = p.company_id "
    "JOIN applicants ap ON ap.id = a.applicant_id "
    "LEFT JOIN matches m ON m.posting_id = a.posting_id AND m.applicant_id = a.applicant_id ")


def _latest_outcomes(app_ids: list[int]) -> dict[int, str]:
    if not app_ids:
        return {}
    conn = connect()
    try:
        qmarks = ",".join("?" * len(app_ids))
        rows = conn.execute(
            f"SELECT application_id, outcome_type FROM outcomes "
            f"WHERE application_id IN ({qmarks}) ORDER BY occurred_at", app_ids).fetchall()
    finally:
        conn.close()
    return {r["application_id"]: r["outcome_type"].replace("_", " ") for r in rows}


@require_GET
def landing(request):
    """Public front door: what this is, how to onboard, what to expect,
    how to read your matches. No job data, no internal links."""
    return render(request, "landing.html", {"hide_internal_nav": True})


@require_GET
def today(request):
    apps = _rows(APP_QUERY + "WHERE a.state IN ('PENDING_REVIEW','NEEDS_HUMAN') "
                 "ORDER BY m.score DESC, a.created_at DESC")
    for a in apps:
        answers = json.loads(a.get("answers_json") or "{}")
        a["gaps"] = sum(1 for f in answers.values()
                        if f.get("required") and (f.get("unknown") or not f.get("value")))
        a["reasons"] = json.loads(a.get("reasons_json") or "[]")
        a["highlights"] = json.loads(a.get("highlights_json") or "[]")
    latest = _latest_outcomes([a["id"] for a in apps])
    for a in apps:
        a["outcome"] = latest.get(a["id"], "")
    return render(request, "today.html", {"apps": apps})


def _timeline(app_id: int) -> list[dict]:
    """State transitions and outcomes for one application, oldest first."""
    conn = connect()
    try:
        events = conn.execute(
            "SELECT created_at, event_type, payload_json FROM events "
            "WHERE application_id = ? AND (event_type LIKE 'transition:%' "
            "OR event_type LIKE 'outcome:%') ORDER BY created_at", (app_id,)).fetchall()
        outcomes = conn.execute(
            "SELECT occurred_at, outcome_type, source, email_subject FROM outcomes "
            "WHERE application_id = ? ORDER BY occurred_at", (app_id,)).fetchall()
    finally:
        conn.close()
    items = [{"at": e["created_at"], "kind": "state",
              "label": e["event_type"].split(":", 1)[1]} for e in events
             if e["event_type"].startswith("transition:")]
    items += [{"at": o["occurred_at"], "kind": "outcome",
               "label": o["outcome_type"].replace("_", " "),
               "detail": o["email_subject"] or f"recorded {o['source']}ly"}
              for o in outcomes]
    return sorted(items, key=lambda x: x["at"])


@require_GET
def app_detail(request, app_id: int):
    """Internal review view: shows ALL fields (incl. salary/visa), editable,
    approvable. This is the private dashboard, not the shared /job_matches page."""
    rows = _rows(APP_QUERY + "WHERE a.id = ?", (app_id,))
    if not rows:
        return HttpResponse("not found", status=404)
    app = rows[0]
    answers = json.loads(app.get("answers_json") or "{}")
    fields = [{"key": k, **v} for k, v in answers.items()]
    gaps = [f for f in fields if f.get("required") and (f.get("unknown") or not f.get("value"))]
    return render(request, "detail.html", {
        "app": app, "fields": fields, "gaps": gaps,
        "public_view": False, "redacted_n": 0,
        "reasons": json.loads(app.get("reasons_json") or "[]"),
        "highlights": json.loads(app.get("highlights_json") or "[]"),
        "alignment": json.loads(app.get("alignment_json") or "[]"),
        "links": _posting_links(app),
        "can_approve": app["state"] == PENDING_REVIEW and not gaps,
        "timeline": _timeline(app_id),
    })


def _salary_signal_for(user_ref: str, app: dict) -> str:
    """Deterministic salary comparison against THIS user's expectation."""
    from .. import analytics
    from ..profile import ProfileError, load_applicant_profile
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM applicants WHERE user_ref = ?",
                           (user_ref,)).fetchone()
    finally:
        conn.close()
    if not row:
        return ""
    try:
        profile = load_applicant_profile(row)
    except (ProfileError, FileNotFoundError):
        return ""
    return analytics.salary_signal(app.get("description_text") or "",
                                   profile.eligibility.salary.min)


@require_GET
def user_matches(request, user_ref: str):
    """List a user's matches by user_ref. Anonymous by design: the page shows
    jobs, scores and match rationale — no name, no contact details, no
    application answers, and salary only as a relative signal."""
    q, sort, order = _list_params(request)
    where, params = "WHERE ap.user_ref = ?", [user_ref]
    if q:
        where += " AND p.title LIKE ?"
        params.append(f"%{q}%")
    rows = _rows(APP_QUERY + where + f" ORDER BY {order}", tuple(params))
    if rows is None:
        rows = []
    for a in rows:
        a["reasons"] = json.loads(a.get("reasons_json") or "[]")
        a["highlights"] = json.loads(a.get("highlights_json") or "[]")
    return render(request, "user_matches.html",
                  {"rows": rows, "user_ref": user_ref, "q": q, "sort": sort,
                   "hide_internal_nav": True})


@require_GET
def job_match_detail(request, user_ref: str, job_id: int):
    """Detail for one (user, job) match. job_id is the posting id — the public,
    stable identifier the email links to: /job_matches/<user_ref>/<job_id>."""
    rows = _rows(APP_QUERY + "WHERE ap.user_ref = ? AND p.id = ?", (user_ref, job_id))
    if not rows:
        return HttpResponse("not found", status=404)
    app = rows[0]
    # Public pages carry ZERO identity and no application answers: just the
    # job, the score, and why it matches. Sensitive comparisons (salary) are
    # rendered as relative signals, never values.
    return render(request, "detail.html", {
        "app": app, "fields": [], "gaps": [],
        "public_view": True, "redacted_n": 0, "hide_internal_nav": True,
        "salary_signal": _salary_signal_for(user_ref, app),
        "reasons": json.loads(app.get("reasons_json") or "[]"),
        "highlights": json.loads(app.get("highlights_json") or "[]"),
        "alignment": json.loads(app.get("alignment_json") or "[]"),
        "links": _posting_links(app),
        "can_approve": False,  # public page is read-only; approval happens on /app/<id>
    })


@require_POST
def app_save(request, app_id: int):
    conn = connect()
    try:
        row = conn.execute("SELECT answers_json, state FROM applications WHERE id = ?",
                           (app_id,)).fetchone()
        if row is None:
            return HttpResponse("not found", status=404)
        if row["state"] not in (PENDING_REVIEW, "NEEDS_HUMAN"):
            return HttpResponseBadRequest("not editable in this state")
        answers = json.loads(row["answers_json"] or "{}")
        for key, meta in answers.items():
            posted = request.POST.get(f"answer__{key}")
            if posted is not None and posted != meta.get("value"):
                meta["value"] = posted
                meta["unknown"] = False
                meta["source"] = "human"
                meta["llm"] = False
        cover = request.POST.get("cover_letter")
        conn.execute("UPDATE applications SET answers_json = ?, "
                     "cover_letter_text = COALESCE(?, cover_letter_text), updated_at = ? "
                     "WHERE id = ?", (json.dumps(answers), cover, now(), app_id))
        conn.commit()
    finally:
        conn.close()
    return redirect(f"/app/{app_id}")


@require_POST
def app_approve(request, app_id: int):
    conn = connect()
    try:
        row = conn.execute("SELECT answers_json, state FROM applications WHERE id = ?",
                           (app_id,)).fetchone()
        if row is None:
            return HttpResponse("not found", status=404)
        answers = json.loads(row["answers_json"] or "{}")
        gaps = [k for k, f in answers.items()
                if f.get("required") and (f.get("unknown") or not f.get("value"))]
        if gaps:
            return HttpResponseBadRequest(f"cannot approve: unresolved fields {gaps}")
        try:
            transition(conn, app_id, APPROVED, payload={"via": "dashboard"})
        except IllegalTransition as e:
            return HttpResponseBadRequest(str(e))
    finally:
        conn.close()
    return redirect("/")


@require_POST
def app_reject(request, app_id: int):
    conn = connect()
    try:
        try:
            transition(conn, app_id, REJECTED_HUMAN,
                       payload={"note": request.POST.get("note", "")})
        except IllegalTransition as e:
            return HttpResponseBadRequest(str(e))
    finally:
        conn.close()
    return redirect("/")


@require_POST
def app_resume(request, app_id: int):
    """NEEDS_HUMAN -> SUBMITTING after the human has fixed whatever blocked it."""
    conn = connect()
    try:
        try:
            transition(conn, app_id, SUBMITTING, payload={"via": "dashboard resume"})
        except IllegalTransition as e:
            return HttpResponseBadRequest(str(e))
    finally:
        conn.close()
    return redirect(f"/app/{app_id}")


@require_GET
def all_postings(request):
    """Every application across ALL users. Scores are per-profile, so the
    user column + filter matter: the same job scores differently per person."""
    from datetime import datetime

    from .. import analytics

    state = request.GET.get("state", "")
    min_score = request.GET.get("min_score", "")
    user = request.GET.get("user", "")
    where, params = [], []
    if state:
        where.append("a.state = ?")
        params.append(state)
    if min_score.isdigit():
        where.append("m.score >= ?")
        params.append(int(min_score))
    if user:
        where.append("ap.user_ref = ?")
        params.append(user)
    q, sort, order = _list_params(request)
    if q:
        where.append("p.title LIKE ?")
        params.append(f"%{q}%")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = _rows(
        "SELECT a.id, a.state, a.created_at, c.name AS company, p.title,"
        "       p.apply_url, p.description_text, m.score,"
        "       ap.name AS user_name, ap.user_ref "
        "FROM applications a JOIN postings p ON p.id = a.posting_id "
        "JOIN companies c ON c.id = p.company_id "
        "JOIN applicants ap ON ap.id = a.applicant_id "
        "LEFT JOIN matches m ON m.posting_id = a.posting_id "
        "AND m.applicant_id = a.applicant_id "
        f"{clause} ORDER BY {order} LIMIT 500", tuple(params))
    for r in rows:
        try:
            r["date"] = datetime.fromisoformat(r["created_at"]).strftime("%b %-d, %Y")
        except ValueError:
            r["date"] = (r["created_at"] or "")[:10]
        r["salary"] = analytics.salary_band(r.get("description_text") or "")
    users = _rows("SELECT name, user_ref FROM applicants WHERE active = 1 ORDER BY name")
    return render(request, "all.html", {"q": q, "sort": sort, "rows": rows,
                  "state": state, "min_score": min_score, "user": user, "users": users})


@require_GET
def healthz(request):
    return HttpResponse("ok", content_type="text/plain")


def stats(request):
    """Analytics: funnel, score-band outcomes, role focus. Private.
    ?applicant=<id> scopes everything to one applicant."""
    from .. import analytics
    applicant_id = request.GET.get("applicant")
    applicant_id = int(applicant_id) if applicant_id and applicant_id.isdigit() else None
    conn = connect()
    try:
        applicant_name = None
        if applicant_id:
            row = conn.execute("SELECT name FROM applicants WHERE id = ?",
                               (applicant_id,)).fetchone()
            applicant_name = row["name"] if row else None
        f = analytics.funnel(conn, applicant_id)
        ctx = {
            "funnel": f,
            "score_bands": analytics.by_score_band(f),
            "roles": analytics.by_role(f),
            "unlinked": analytics.unlinked_outcomes(conn),
            "applicant_id": applicant_id, "applicant_name": applicant_name,
        }
    finally:
        conn.close()
    return render(request, "stats.html", ctx)


def outcome_link(request, outcome_id: int):
    """Link an unlinked forwarded email to an application (from /stats)."""
    if request.method != "POST":
        return HttpResponse(status=405)
    app_id = request.POST.get("application_id", "").strip()
    if not app_id.isdigit():
        return HttpResponse("application_id required", status=400)
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM applications WHERE id = ?",
                            (int(app_id),)).fetchone():
            return HttpResponse("no such application", status=404)
        with tx(conn):
            conn.execute("UPDATE outcomes SET application_id = ? WHERE id = ?",
                         (int(app_id), outcome_id))
    finally:
        conn.close()
    return redirect("/stats")


@require_GET
def sources(request):
    """Which sources are worth having? Uniqueness, overlap, freshness and
    match quality per source, plus operational health. Private."""
    from .. import source_analytics
    from ..sources import registry

    conn = connect()
    try:
        overlap = source_analytics.overlap_matrix(conn)
        volume = source_analytics.volume_by_day(conn)
        ctx = {
            "health": registry.health(conn),
            "summary": source_analytics.source_summary(conn),
            "overlap_sources": overlap["sources"],
            "overlap_rows": [
                {"source": a, "total": overlap["totals"][a],
                 "cells": [overlap["rows"][a][b] for b in overlap["sources"]]}
                for a in overlap["sources"]],
            "volume_days": [d[5:] for d in volume["days"]],   # MM-DD
            "volume_rows": sorted(volume["series"].items()),
        }
    finally:
        conn.close()
    return render(request, "sources.html", ctx)


@require_GET
def applicants_index(request):
    """The map: every applicant, their pipeline counts, links to their
    matches page and per-applicant stats."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT ap.id, ap.name, ap.user_ref, ap.active, "
            "  (SELECT COUNT(*) FROM matches m WHERE m.applicant_id = ap.id) AS n_matches, "
            "  (SELECT COUNT(*) FROM applications a WHERE a.applicant_id = ap.id "
            "     AND a.state IN ('MATCHED','PREPARED','PENDING_REVIEW')) AS n_review, "
            "  (SELECT COUNT(*) FROM applications a WHERE a.applicant_id = ap.id "
            "     AND a.state IN ('SUBMITTED','CONFIRMED')) AS n_applied, "
            "  (SELECT COUNT(DISTINCT o.application_id) FROM outcomes o "
            "     JOIN applications a2 ON a2.id = o.application_id "
            "     WHERE a2.applicant_id = ap.id "
            "     AND o.outcome_type IN ('interview_invite','assessment')) AS n_interviews "
            "FROM applicants ap ORDER BY ap.active DESC, ap.name").fetchall()
    finally:
        conn.close()
    return render(request, "applicants.html", {"applicants": [dict(r) for r in rows]})


@require_POST
def app_applied(request, app_id: int):
    """Mark an application as manually submitted (you applied yourself).
    Enters the same SUBMITTED state as the automated path, so email outcome
    linking, timelines and stats all work identically."""
    conn = connect()
    try:
        try:
            transition(conn, app_id, "SUBMITTED",
                       payload={"manual": True, "at": now()})
        except IllegalTransition as e:
            return HttpResponseBadRequest(str(e))
    finally:
        conn.close()
    return redirect(f"/app/{app_id}")


USERNAME_RE = __import__("re").compile(r"^[A-Za-z0-9_-]{1,30}$")


def _username_error(conn, name: str) -> str:
    """Validate a signup username destined to be the public page ref."""
    if not USERNAME_RE.match(name or ""):
        return ("user name must be 1-30 characters: letters, numbers, "
                "hyphen or underscore only (it becomes your page URL)")
    ref = name.lower()
    if conn.execute("SELECT 1 FROM applicants WHERE lower(user_ref) = ? "
                    "OR lower(name) = ?", (ref, ref)).fetchone():
        return "that user name is taken — pick another"
    return ""


def onboard(request):
    """Public onboarding: a new user describes what they want; we build and
    validate a Profile, store it on their applicant row, and hand back their
    personal matches link. Matching starts only when Joe kicks it off
    (scripts or the scheduled run - applicants start INACTIVE)."""

    import yaml as _yaml

    from ..profile import ProfileError, load_profile_yaml

    if request.method == "GET":
        return render(request, "onboard.html", {"hide_internal_nav": True})

    f = request.POST
    # Abuse guards: hard length caps before anything is parsed or stored.
    CAPS = {"yaml_override": 20000, "positioning": 4000, "experience": 4000}
    for field, cap in CAPS.items():
        if len(f.get(field) or "") > cap:
            return render(request, "onboard.html",
                          {"error": f"{field} is too long (max {cap} characters)",
                           "form": f}, status=400)
    if any(len(f.get(k) or "") > 300 for k in
           ("full_name", "email", "location", "target_titles", "title_synonyms",
            "locations", "hard_nos", "skills", "link_linkedin", "link_github",
            "link_portfolio")):
        return render(request, "onboard.html",
                      {"error": "one of the short fields is too long", "form": f,
                       "hide_internal_nav": True},
                      status=400)
    # Advanced path: a pasted profile YAML wins outright and is stored
    # verbatim — full fidelity, including sections beyond the core schema
    # (experience, skills, projects). The matcher reads the raw YAML.
    yaml_override = (f.get("yaml_override") or "").strip()
    if yaml_override:
        try:
            prof = load_profile_yaml(yaml_override)
        except ProfileError as e:
            return render(request, "onboard.html",
                          {"error": str(e), "form": f, "hide_internal_nav": True}, status=400)
        conn = connect()
        try:
            err = _username_error(conn, prof.identity.full_name)
            if err:
                return render(request, "onboard.html",
                              {"error": err, "form": f, "hide_internal_nav": True},
                              status=400)
            user_ref = prof.identity.full_name.lower()
            with tx(conn):
                conn.execute(
                    "INSERT INTO applicants (name, profile_path, profile_yaml,"
                    " user_ref, active) VALUES (?, '', ?, ?, 0)",
                    (prof.identity.full_name, yaml_override, user_ref))
        finally:
            conn.close()
        return render(request, "onboard_done.html",
                      {"name": prof.identity.full_name, "user_ref": user_ref,
                       "hide_internal_nav": True})

    titles = [t.strip() for t in (f.get("target_titles") or "").split(",") if t.strip()]
    locations = [loc.strip() for loc in (f.get("locations") or "London").split(",") if loc.strip()]
    links = {k: (f.get(f"link_{k}") or "").strip()
             for k in ("linkedin", "github", "portfolio")}
    data = {
        "identity": {
            "full_name": (f.get("full_name") or "").strip(),
            "email": (f.get("email") or "").strip(),
            "location": (f.get("location") or "").strip(),
            "links": {k: u for k, u in links.items() if u},
        },
        "preferences": {
            "target_titles": titles,
            "title_synonyms": [t.strip() for t in (f.get("title_synonyms") or "").split(",") if t.strip()],
            "locations_ok": locations,
            "hard_nos": [t.strip() for t in (f.get("hard_nos") or "").split(",") if t.strip()],
        },
        "positioning_summary": (f.get("positioning") or "").strip(),
        "experience": (f.get("experience") or "").strip(),
        "skills": [s.strip() for s in (f.get("skills") or "").split(",") if s.strip()],
        # Discovery-scope onboarding: no visa/notice fields. Salary min only
        # powers the relative "above/below expectations" signal.
        "eligibility": {
            "salary": {"currency": "GBP",
                       "min": int(f["salary_min"]) if (f.get("salary_min") or "").isdigit() else None,
                       "preferred": int(f["salary_pref"]) if (f.get("salary_pref") or "").isdigit() else None},
        },
    }
    yaml_text = _yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    try:
        load_profile_yaml(yaml_text)   # full pydantic validation before storing
    except ProfileError as e:
        return render(request, "onboard.html",
                      {"error": str(e), "form": f, "hide_internal_nav": True}, status=400)
    if not data["identity"]["full_name"]:
        return render(request, "onboard.html",
                      {"error": "name is required", "form": f, "hide_internal_nav": True}, status=400)
    conn = connect()
    try:
        err = _username_error(conn, data["identity"]["full_name"])
        if err:
            return render(request, "onboard.html",
                          {"error": err, "form": f, "hide_internal_nav": True},
                          status=400)
        user_ref = data["identity"]["full_name"].lower()
        with tx(conn):
            conn.execute(
                "INSERT INTO applicants (name, profile_path, profile_yaml,"
                " user_ref, active) VALUES (?, '', ?, ?, 0)",
                (data["identity"]["full_name"], yaml_text, user_ref))
    finally:
        conn.close()
    return render(request, "onboard_done.html",
                  {"name": data["identity"]["full_name"], "user_ref": user_ref,
                   "hide_internal_nav": True})


@require_GET
def go_to_matches(request):
    """Landing-page helper: type your user name, land on your matches."""
    name = (request.GET.get("u") or "").strip().lower()
    if not USERNAME_RE.match(name):
        return render(request, "landing.html",
                      {"hide_internal_nav": True,
                       "go_error": "user names are 1-30 letters, numbers, - or _"},
                      status=400)
    return redirect(f"/job_matches/{name}")
