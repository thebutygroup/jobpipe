"""Analytics over the whole pipeline: what worked, what didn't.

Everything is derived at read time from postings/matches/applications/
outcomes — no denormalised counters to drift. Salary is extracted
opportunistically from posting text (GBP ranges) since boards rarely
provide it structured. Industry needs company-page enrichment (roadmap).
"""
from __future__ import annotations

import re

APPLIED_STATES = ("SUBMITTED", "CONFIRMED")

ROLE_BUCKETS = [
    ("Forward Deployed / FDE", re.compile(r"forward.?deploy|\bfde\b", re.I)),
    ("Data Engineering", re.compile(r"data engineer|analytics engineer|etl|pipeline", re.I)),
    ("ML / AI Engineering", re.compile(r"\bml\b|machine learning|\bai\b|llm|genai", re.I)),
    ("Platform / Infra", re.compile(r"platform|infra|devops|sre|reliability", re.I)),
    ("Solutions / Customer", re.compile(r"solutions?|customer|success|implementation", re.I)),
]

_SALARY = re.compile(r"£\s?(\d{2,3})(?:[,.](\d{3})|(k))?\s*(?:-|to|–)\s*£?\s?(\d{2,3})(?:[,.](\d{3})|(k))?", re.I)


def role_bucket(title: str) -> str:
    for name, pat in ROLE_BUCKETS:
        if pat.search(title or ""):
            return name
    return "Other"


def salary_band(text: str) -> str:
    """Best-effort GBP band from posting text: '£70,000 - £90,000' -> '70-90k'."""
    m = _SALARY.search(text or "")
    if not m:
        return ""
    lo = int(m.group(1)) * (1000 if (m.group(2) or m.group(3)) else 1)
    hi = int(m.group(4)) * (1000 if (m.group(5) or m.group(6)) else 1)
    if lo < 10000 or hi < lo:
        return ""
    return f"£{lo // 1000}-{hi // 1000}k"


def salary_signal(description: str, expected_min: int | None) -> str:
    """Relative signal only — never the numbers. Compares the posting's parsed
    GBP range midpoint to the applicant's expectation."""
    if not expected_min:
        return ""
    m = _SALARY.search(description or "")
    if not m:
        return ""
    lo = int(m.group(1)) * (1000 if (m.group(2) or m.group(3)) else 1)
    hi = int(m.group(4)) * (1000 if (m.group(5) or m.group(6)) else 1)
    if lo < 10000 or hi < lo:
        return ""
    mid = (lo + hi) / 2
    ratio = mid / expected_min
    if ratio >= 1.3:
        return "salary far above your expectations"
    if ratio >= 1.08:
        return "salary above your expectations"
    if ratio >= 0.95:
        return "salary in line with your expectations"
    if ratio >= 0.85:
        return "salary slightly below your expectations"
    return "salary below your expectations"


def funnel(conn, applicant_id: int | None = None) -> dict:
    where, params = "", ()
    if applicant_id:
        where, params = " WHERE a.applicant_id = ?", (applicant_id,)
    apps = conn.execute(
        f"SELECT a.id, a.state, m.score, p.title, p.description_text "
        f"FROM applications a JOIN postings p ON p.id = a.posting_id "
        f"LEFT JOIN matches m ON m.posting_id = a.posting_id "
        f"AND m.applicant_id = a.applicant_id{where}", params).fetchall()
    outcome_rows = conn.execute(
        "SELECT o.application_id, o.outcome_type FROM outcomes o "
        "WHERE o.application_id IS NOT NULL").fetchall()
    by_app: dict[int, set] = {}
    for r in outcome_rows:
        by_app.setdefault(r["application_id"], set()).add(r["outcome_type"])

    applied = [a for a in apps if a["state"] in APPLIED_STATES]
    interviewed = [a for a in applied
                   if by_app.get(a["id"], set()) & {"interview_invite", "assessment"}]
    return {
        "total_apps": len(apps),
        "applied": len(applied),
        "interviews": len(interviewed),
        "offers": sum(1 for a in applied if "offer" in by_app.get(a["id"], set())),
        "rejected": sum(1 for a in applied if "rejected" in by_app.get(a["id"], set())),
        "no_response": sum(1 for a in applied if not by_app.get(a["id"])),
        "interview_rate": round(100 * len(interviewed) / len(applied)) if applied else 0,
        "_apps": apps, "_outcomes": by_app,
    }


def by_score_band(f: dict) -> list[dict]:
    """Does the match score predict outcomes? The core aggregate insight."""
    bands: dict[int, dict] = {}
    for a in f["_apps"]:
        if a["state"] not in APPLIED_STATES or a["score"] is None:
            continue
        b = bands.setdefault(a["score"], {"score": a["score"], "applied": 0,
                                          "interviews": 0, "rejected": 0})
        b["applied"] += 1
        outs = f["_outcomes"].get(a["id"], set())
        b["interviews"] += bool(outs & {"interview_invite", "assessment"})
        b["rejected"] += "rejected" in outs
    return sorted(bands.values(), key=lambda b: -b["score"])


def by_role(f: dict) -> list[dict]:
    buckets: dict[str, dict] = {}
    for a in f["_apps"]:
        if a["state"] not in APPLIED_STATES:
            continue
        name = role_bucket(a["title"])
        b = buckets.setdefault(name, {"role": name, "applied": 0, "interviews": 0,
                                      "salaries": []})
        b["applied"] += 1
        b["interviews"] += bool(f["_outcomes"].get(a["id"], set())
                                & {"interview_invite", "assessment"})
        band = salary_band(a["description_text"])
        if band:
            b["salaries"].append(band)
    out = []
    for b in sorted(buckets.values(), key=lambda x: -x["applied"]):
        b["salary_bands"] = ", ".join(sorted(set(b["salaries"]))[:4]) or "—"
        del b["salaries"]
        out.append(b)
    return out


def unlinked_outcomes(conn) -> list:
    return conn.execute(
        "SELECT id, outcome_type, email_subject, company_guess, occurred_at "
        "FROM outcomes WHERE application_id IS NULL "
        "ORDER BY occurred_at DESC LIMIT 20").fetchall()
