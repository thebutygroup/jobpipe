"""Workday poller — the one enterprise system worth polling directly, because
its career sites share a semi-standard JSON endpoint:

    POST {host}/wday/cxs/{tenant}/{site}/jobs
    body: {"limit": N, "offset": N, "searchText": "..."}

Endpoint shape verified per-tenant on first live run (some tenants disable it;
those get demoted to bespoke). We query per target title with searchText rather
than paging entire boards — a 20k-role bank is not something to mirror weekly.
"""

from __future__ import annotations

import logging

import requests

from ..config import settings
from ..models import PostingDTO

log = logging.getLogger(__name__)


def fetch(company_name: str, host: str, tenant: str, site: str,
          search_terms: list[str], limit: int = 20) -> list[PostingDTO]:
    url = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    out, seen = [], set()
    for term in search_terms:
        try:
            resp = requests.post(url, json={"limit": limit, "offset": 0,
                                            "searchText": term},
                                 headers={"User-Agent": settings.user_agent,
                                          "Content-Type": "application/json"},
                                 timeout=30)
            if resp.status_code != 200:
                log.warning("workday %s returned %s for %r", tenant,
                            resp.status_code, term)
                continue
            for job in resp.json().get("jobPostings", []):
                dto = normalise(company_name, host, site, job)
                if dto.external_id not in seen:
                    seen.add(dto.external_id)
                    out.append(dto)
        except (requests.RequestException, ValueError):
            log.exception("workday fetch failed: %s %r", tenant, term)
    return out


def normalise(company_name: str, host: str, site: str, job: dict) -> PostingDTO:
    external_path = job.get("externalPath", "")
    return PostingDTO(
        company_name=company_name,
        source="ats",
        external_id=job.get("bulletFields", [""])[0] or external_path,
        title=job.get("title", ""),
        location=job.get("locationsText", ""),
        apply_url=f"{host}/{site}{external_path}" if external_path else host,
        description_text=job.get("title", ""),  # detail fetch deferred to prepare stage
        raw=job,
    )
