"""Greenhouse public board API.

Endpoint (verify live before trusting a token):
GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
"""

from __future__ import annotations

from ..models import PostingDTO
from .base import polite_get, strip_html

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def fetch_raw(board_token: str) -> list[dict]:
    return polite_get(API.format(token=board_token)).json().get("jobs", [])


def fetch(company_name: str, board_token: str) -> list[PostingDTO]:
    return [normalise(company_name, j) for j in fetch_raw(board_token)]


def normalise(company_name: str, job: dict) -> PostingDTO:
    return PostingDTO(
        company_name=company_name,
        source="ats",
        external_id=str(job.get("id", "")),
        title=job.get("title", ""),
        location=(job.get("location") or {}).get("name", ""),
        department=", ".join(d.get("name", "") for d in job.get("departments") or []),
        apply_url=job.get("absolute_url", ""),
        description_text=strip_html(job.get("content", "")),
        raw=job,
    )
