"""Asset vault: per-applicant document storage that is never in git, never in
the image, and never served by any web route.

Layout: <settings.assets_root>/<vault_token>/<generated filename>
- assets_root lives under data/ (volume-mounted, gitignored)
- vault_token is a random urlsafe token, NOT the username — path unguessable
- metadata (variant names, hashes) lives in the assets table; the dashboard
  may show metadata, but file bytes are read ONLY by the submitter at
  submission time.

Variants: resumes are keyed by variant_name ('default', 'mlops', 'fde'…).
resume_for(title) picks the variant whose name appears in the job title,
falling back to 'default' — the "different resumes for different position
types" mechanic.
"""

from __future__ import annotations

import hashlib
import pathlib
import secrets
import shutil

from ..config import settings
from ..db import now, tx


class VaultError(Exception):
    pass


class AssetVault:
    def __init__(self, conn, applicant_id: int):
        self.conn = conn
        self.applicant_id = applicant_id
        self.token = self._ensure_token()

    def _ensure_token(self) -> str:
        row = self.conn.execute("SELECT vault_token FROM applicants WHERE id = ?",
                                (self.applicant_id,)).fetchone()
        if row is None:
            raise VaultError(f"no applicant {self.applicant_id}")
        if row["vault_token"]:
            return row["vault_token"]
        token = secrets.token_urlsafe(12)
        with tx(self.conn):
            self.conn.execute("UPDATE applicants SET vault_token = ? WHERE id = ?",
                              (token, self.applicant_id))
        return token

    @property
    def root(self) -> pathlib.Path:
        return pathlib.Path(settings.assets_root) / self.token

    def add(self, file_path: str, kind: str = "resume",
            variant_name: str = "default") -> dict:
        """Copy a file into the vault under our own name; upsert metadata.
        Re-adding the same (kind, variant) replaces it."""
        src = pathlib.Path(file_path)
        if not src.is_file():
            raise VaultError(f"not a file: {file_path}")
        ext = src.suffix.lower()
        if kind == "resume" and ext not in (".pdf", ".doc", ".docx", ".txt", ".rtf"):
            raise VaultError(f"unsupported resume type {ext} "
                             "(portals accept pdf/doc/docx/txt/rtf)")
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        stored_name = f"{kind}-{variant_name}{ext}"
        shutil.copyfile(src, self.root / stored_name)
        with tx(self.conn):
            self.conn.execute(
                "INSERT INTO assets (applicant_id, kind, variant_name, filename,"
                " original_name, content_sha256, size, uploaded_at)"
                " VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(applicant_id, kind, variant_name) DO UPDATE SET"
                " filename=excluded.filename, original_name=excluded.original_name,"
                " content_sha256=excluded.content_sha256, size=excluded.size,"
                " uploaded_at=excluded.uploaded_at",
                (self.applicant_id, kind, variant_name, stored_name, src.name,
                 digest, src.stat().st_size, now()))
        return {"variant": variant_name, "stored": stored_name, "sha256": digest}

    def list(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT kind, variant_name, filename, original_name, size, uploaded_at"
            " FROM assets WHERE applicant_id = ? ORDER BY kind, variant_name",
            (self.applicant_id,))]

    def resume_for(self, job_title: str) -> pathlib.Path | None:
        """Pick the resume variant for a job title: a variant whose name
        appears as a word in the title wins; otherwise 'default'."""
        rows = self.conn.execute(
            "SELECT variant_name, filename FROM assets"
            " WHERE applicant_id = ? AND kind = 'resume'",
            (self.applicant_id,)).fetchall()
        if not rows:
            return None
        title = (job_title or "").lower()
        by_variant = {r["variant_name"]: r["filename"] for r in rows}
        for variant, filename in by_variant.items():
            if variant != "default" and variant.lower() in title:
                return self.root / filename
        if "default" in by_variant:
            return self.root / by_variant["default"]
        # no default: any variant beats nothing
        return self.root / rows[0]["filename"]
