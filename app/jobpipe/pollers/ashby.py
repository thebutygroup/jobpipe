"""Ashby public posting API.

Endpoint (verify live): GET https://api.ashbyhq.com/posting-api/job-board/{token}
"""

from __future__ import annotations

from ..models import PostingDTO
from .base import polite_get, strip_html

API = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"


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
        location=job.get("location", ""),
        remote_policy="remote" if job.get("isRemote") else "",
        department=job.get("department", ""),
        apply_url=job.get("jobUrl", "") or job.get("applyUrl", ""),
        description_text=strip_html(job.get("descriptionHtml", "")),
        raw=job,
    )
