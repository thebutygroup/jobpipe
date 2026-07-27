"""The Application aggregate — the one screen a new contributor reads first.

    Application
    ├── applicant: Applicant   (who — profile + asset vault)
    ├── job: Job               (what — posting + how to apply, applicant-free)
    ├── answers                (prepared field values; persisted on the row)
    └── state                  (the existing DISCOVERED…CONFIRMED machine)

Everything is loaded from the existing tables; this module adds structure,
not storage. Platform-specific behaviour hangs off job.route.platform via
apply.platforms.get_applier().
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..profile import Profile, load_applicant_profile
from .platforms import get_applier
from .routes import ApplyRoute, ensure_route
from .vault import AssetVault


@dataclass
class Applicant:
    id: int
    user_ref: str
    name: str
    profile: Profile
    vault: AssetVault

    @classmethod
    def load(cls, conn, applicant_id: int) -> "Applicant":
        row = conn.execute("SELECT * FROM applicants WHERE id = ?",
                           (applicant_id,)).fetchone()
        if row is None:
            raise ValueError(f"no applicant {applicant_id}")
        return cls(id=row["id"], user_ref=row["user_ref"] or "", name=row["name"],
                   profile=load_applicant_profile(row),
                   vault=AssetVault(conn, row["id"]))


@dataclass
class Job:
    posting_id: int
    company: str
    title: str
    location: str
    description: str
    listing_url: str
    route: ApplyRoute

    @property
    def applier(self):
        return get_applier(self.route.platform)

    @classmethod
    def load(cls, conn, posting_id: int, **route_kw) -> "Job":
        row = conn.execute(
            "SELECT p.*, c.name AS company FROM postings p"
            " JOIN companies c ON c.id = p.company_id WHERE p.id = ?",
            (posting_id,)).fetchone()
        if row is None:
            raise ValueError(f"no posting {posting_id}")
        return cls(posting_id=row["id"], company=row["company"], title=row["title"],
                   location=row["location"] or "",
                   description=row["description_text"] or "",
                   listing_url=row["canonical_apply_url"] or row["apply_url"] or "",
                   route=ensure_route(conn, posting_id, **route_kw))


@dataclass
class Application:
    id: int
    job: Job
    applicant: Applicant
    state: str
    answers: dict = field(default_factory=dict)
    cover_letter: str = ""
    resume_variant: str = ""

    @classmethod
    def load(cls, conn, application_id: int, **route_kw) -> "Application":
        row = conn.execute("SELECT * FROM applications WHERE id = ?",
                           (application_id,)).fetchone()
        if row is None:
            raise ValueError(f"no application {application_id}")
        return cls(
            id=row["id"],
            job=Job.load(conn, row["posting_id"], **route_kw),
            applicant=Applicant.load(conn, row["applicant_id"]),
            state=row["state"],
            answers=json.loads(row["answers_json"] or "{}"),
            cover_letter=row["cover_letter_text"] or "",
            resume_variant=row["resume_variant"] or "")

    @property
    def resume_path(self):
        """The vault resume chosen for THIS job's title (variant mechanic)."""
        return self.applicant.vault.resume_for(self.job.title)
