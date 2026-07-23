"""Lever public postings API.

Endpoint (verify live): GET https://api.lever.co/v0/postings/{token}?mode=json
"""

from __future__ import annotations

from ..models import PostingDTO
from .base import polite_get, strip_html

API = "https://api.lever.co/v0/postings/{token}?mode=json"


def fetch_raw(board_token: str) -> list[dict]:
    return polite_get(API.format(token=board_token)).json()


def fetch(company_name: str, board_token: str) -> list[PostingDTO]:
    return [normalise(company_name, j) for j in fetch_raw(board_token)]


def normalise(company_name: str, job: dict) -> PostingDTO:
    cats = job.get("categories") or {}
    return PostingDTO(
        company_name=company_name,
        source="ats",
        external_id=str(job.get("id", "")),
        title=job.get("text", ""),
        location=cats.get("location", ""),
        remote_policy=cats.get("commitment", ""),
        department=cats.get("team", ""),
        apply_url=job.get("hostedUrl", ""),
        description_text=strip_html(job.get("description", "")) or job.get("descriptionPlain", ""),
        raw=job,
    )
