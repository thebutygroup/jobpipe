"""Self-serve profile viewing/editing — the safe way.

Users NEVER upload or paste YAML. They fill structured text boxes with hard
character limits; the server rebuilds the YAML, validates it against the
Profile schema, and stores it. Every field passes the injection screen
(jobpipe.safety) first — a flagged submission shadow-bans silently.

Access control: the edit URL carries a per-user secret token
(/profile/<user_ref>/<token>) that only ever travels in email to the address
on their profile. No token, no page.
"""

from __future__ import annotations

import logging
import secrets

import yaml

from .profile import ProfileError, load_profile_yaml

log = logging.getLogger(__name__)

# (field, label, kind, placeholder) — kind: input | textarea | number
FORM_FIELDS = [
    ("email", "Email", "input", "you@example.com"),
    ("target_titles", "Job titles you want (comma-separated, 3-5 is ideal)",
     "input", "Head of Data, Data Director, Analytics Lead"),
    ("title_synonyms", "Also-acceptable title variants (comma-separated)",
     "input", "Head of Insight, BI Lead"),
    ("positioning", "What are you looking for? (drives the matching)",
     "textarea", "I'm a ... looking for ... What matters most is ..."),
    ("experience", "Experience — roles, years, standout achievements",
     "textarea", "8 years in ... led a team of ... shipped ..."),
    ("skills", "Skills (comma-separated)", "input",
     "stakeholder management, SQL, team building"),
    ("locations_ok", "Locations that work (comma-separated)", "input",
     "London, Hybrid, Remote (UK)"),
    ("hard_nos", "Deal-breakers (comma-separated)", "input",
     "fully on-site, relocation required"),
    ("salary_min", "Minimum salary (GBP, number only — never shown publicly)",
     "number", "60000"),
]


def ensure_edit_token(conn, applicant_id: int) -> str:
    row = conn.execute("SELECT edit_token FROM applicants WHERE id = ?",
                       (applicant_id,)).fetchone()
    if row and row["edit_token"]:
        return row["edit_token"]
    token = secrets.token_urlsafe(16)
    conn.execute("UPDATE applicants SET edit_token = ? WHERE id = ?",
                 (token, applicant_id))
    conn.commit()
    return token


def _split(csv: str) -> list[str]:
    return [p.strip() for p in (csv or "").split(",") if p.strip()]


def titles_from_row(row) -> list[str]:
    """The target titles stored on an applicant row, best-effort ([] on any
    parse trouble — display-only callers must never crash on bad YAML)."""
    import yaml
    try:
        data = yaml.safe_load(row["profile_yaml"] or "") or {}
        return list((data.get("preferences") or {}).get("target_titles") or [])
    except Exception:
        return []


def is_quick_only(row) -> bool:
    """True while a profile is still the Quick-match minimum (titles + one
    sentence): no skills, no experience, no title variants. Drives the
    'upgrade to the Full match' nudges — and stops them the moment the user
    adds anything."""
    import yaml
    try:
        data = yaml.safe_load(row["profile_yaml"] or "") or {}
    except Exception:
        return False
    prefs = data.get("preferences") or {}
    return not (data.get("skills") or data.get("experience")
                or prefs.get("title_synonyms"))


def fields_from_row(row) -> dict[str, str]:
    """Prefill form fields from the stored profile YAML (empty-safe)."""
    try:
        data = yaml.safe_load(row["profile_yaml"] or "") or {}
    except yaml.YAMLError:
        data = {}
    prefs = data.get("preferences") or {}
    salary = ((data.get("eligibility") or {}).get("salary") or {})
    return {
        "email": (data.get("identity") or {}).get("email") or "",
        "target_titles": ", ".join(prefs.get("target_titles") or []),
        "title_synonyms": ", ".join(prefs.get("title_synonyms") or []),
        "positioning": data.get("positioning_summary") or "",
        "experience": data.get("experience") or "",
        "skills": ", ".join(data.get("skills") or [])
                  if isinstance(data.get("skills"), list)
                  else (data.get("skills") or ""),
        "locations_ok": ", ".join(prefs.get("locations_ok") or []),
        "hard_nos": ", ".join(prefs.get("hard_nos") or []),
        "salary_min": str(salary.get("min") or ""),
    }


def apply_fields(existing_yaml: str, fields: dict[str, str]) -> str:
    """Rebuild + VALIDATE the profile YAML from clean form fields. Preserves
    everything the form doesn't manage (full_name, links, machine-added keys).
    Raises ProfileError if the result doesn't validate."""
    data = yaml.safe_load(existing_yaml or "") or {}
    identity = data.setdefault("identity", {})
    identity["email"] = (fields.get("email") or "").strip()
    identity.setdefault("full_name", "")
    identity.setdefault("location", "")
    prefs = data.setdefault("preferences", {})
    prefs["target_titles"] = _split(fields.get("target_titles", ""))
    prefs["title_synonyms"] = _split(fields.get("title_synonyms", ""))
    prefs["locations_ok"] = _split(fields.get("locations_ok", "")) or ["London"]
    prefs["hard_nos"] = _split(fields.get("hard_nos", ""))
    data["positioning_summary"] = (fields.get("positioning") or "").strip()
    data["experience"] = (fields.get("experience") or "").strip()
    data["skills"] = _split(fields.get("skills", ""))
    salary_min = (fields.get("salary_min") or "").strip()
    if salary_min:
        if not salary_min.isdigit():
            raise ProfileError("minimum salary must be a plain number")
        data.setdefault("eligibility", {}).setdefault("salary", {})["min"] = int(salary_min)
    else:
        data.setdefault("eligibility", {}).setdefault("salary", {})["min"] = None
    if not prefs["target_titles"]:
        raise ProfileError("at least one target job title is required")
    dumped = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    load_profile_yaml(dumped)  # schema validation — raises ProfileError
    return dumped
