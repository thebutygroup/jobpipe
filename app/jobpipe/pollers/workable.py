"""Workable public widget API.

Endpoint (verify live):
GET https://apply.workable.com/api/v1/widget/accounts/{token}?details=true
"""

from __future__ import annotations

from ..models import PostingDTO
from .base import polite_get, strip_html

API = "https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"


def fetch(company_name: str, board_token: str) -> list[PostingDTO]:
    data = polite_get(API.format(token=board_token)).json()
    return [normalise(company_name, j) for j in data.get("jobs", [])]


def normalise(company_name: str, job: dict) -> PostingDTO:
    return PostingDTO(
        company_name=company_name,
        source="ats",
        external_id=str(job.get("shortcode", "") or job.get("id", "")),
        title=job.get("title", ""),
        location=", ".join(filter(None, [job.get("city", ""), job.get("country", "")])),
        remote_policy="remote" if job.get("telecommuting") else "",
        department=job.get("department", ""),
        apply_url=job.get("url", "") or job.get("application_url", ""),
        description_text=strip_html(job.get("description", "")),
        raw=job,
    )
