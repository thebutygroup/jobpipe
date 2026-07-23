"""Adzuna aggregator adapter.

Endpoint (verify live): GET https://api.adzuna.com/v1/api/jobs/gb/search/{page}
    ?app_id=...&app_key=...&what=<keywords>&where=<location>&results_per_page=N

Free tier is rate-limited (~250 calls/day). The runner counts calls via
'adzuna_api_call' events and stops at settings.adzuna_daily_call_cap.

Response shape (fixture: tests/fixtures/adzuna_search.json):
    {"count": int, "results": [{"id", "title", "description",
      "company": {"display_name"}, "location": {"display_name", "area": [...]},
      "salary_min", "salary_max", "salary_is_predicted", "redirect_url",
      "created", "contract_type", "contract_time", "category": {...}}, ...]}
"""

from __future__ import annotations

from ..config import settings
from ..models import PostingDTO
from ..pollers.base import polite_get, strip_html
from .base import SearchSpec, SourceAdapter, normalise_location, normalise_salary

API = "https://api.adzuna.com/v1/api/jobs/gb/search/{page}"


class AdzunaAdapter(SourceAdapter):
    name = "adzuna"
    kind = "aggregator"

    def is_configured(self) -> bool:
        return bool(settings.adzuna_app_id and settings.adzuna_app_key)

    def unconfigured_reason(self) -> str:
        return "set ADZUNA_APP_ID and ADZUNA_APP_KEY in app/.env (developer.adzuna.com)"

    def fetch(self, spec: SearchSpec) -> list[dict]:
        results: list[dict] = []
        for page in range(1, settings.aggregator_max_pages + 1):
            data = polite_get(API.format(page=page), params={
                "app_id": settings.adzuna_app_id,
                "app_key": settings.adzuna_app_key,
                "what": spec.keywords,
                "where": spec.location,
                "distance": int(spec.distance_miles * 1.609),  # Adzuna takes km
                "results_per_page": settings.aggregator_results_per_page,
                "content-type": "application/json",
            }).json()
            page_results = data.get("results", [])
            results.extend(page_results)
            if len(page_results) < settings.aggregator_results_per_page:
                break
        return results

    def normalize(self, raw: dict, spec: SearchSpec) -> PostingDTO:
        salary = normalise_salary(raw.get("salary_min"), raw.get("salary_max"))
        contract_time = raw.get("contract_time") or ""
        return PostingDTO(
            company_name=(raw.get("company") or {}).get("display_name", "")
            or "Unknown (adzuna)",
            source=self.name,
            external_id=str(raw.get("id", "")),
            title=raw.get("title", ""),
            location=normalise_location((raw.get("location") or {}).get("display_name", "")),
            remote_policy="",  # Adzuna does not expose a remote flag
            department=(raw.get("category") or {}).get("label", ""),
            apply_url=raw.get("redirect_url", ""),
            description_text=strip_html(raw.get("description", "")),
            raw={**raw, "salary_normalised": salary, "posted_at": raw.get("created", ""),
                 "contract_time": contract_time, "search": spec.label},
        )
