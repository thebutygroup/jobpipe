"""Reed.co.uk aggregator adapter.

Endpoint (verify live): GET https://www.reed.co.uk/api/1.0/search
    ?keywords=...&locationName=...&distanceFromLocation=15
Auth: HTTP Basic, API key as username, blank password.

Response shape (fixture: tests/fixtures/reed_search.json):
    {"totalResults": int, "results": [{"jobId", "employerName", "jobTitle",
      "locationName", "minimumSalary", "maximumSalary", "currency",
      "expirationDate", "date", "jobDescription", "jobUrl", ...}, ...]}
Salary fields are omitted when the employer hides them.
"""

from __future__ import annotations

from ..config import settings
from ..models import PostingDTO
from ..pollers.base import polite_get, strip_html
from .base import SearchSpec, SourceAdapter, normalise_location, normalise_salary

API = "https://www.reed.co.uk/api/1.0/search"


class ReedAdapter(SourceAdapter):
    name = "reed"
    kind = "aggregator"

    def is_configured(self) -> bool:
        return bool(settings.reed_api_key)

    def unconfigured_reason(self) -> str:
        return "set REED_API_KEY in app/.env (reed.co.uk/developers/jobseeker)"

    def fetch(self, spec: SearchSpec) -> list[dict]:
        results: list[dict] = []
        take = settings.aggregator_results_per_page
        for page in range(settings.aggregator_max_pages):
            data = polite_get(API, auth=(settings.reed_api_key, ""), params={
                "keywords": spec.keywords,
                "locationName": spec.location,
                "distanceFromLocation": spec.distance_miles,
                "resultsToTake": take,
                "resultsToSkip": page * take,
            }).json()
            page_results = data.get("results", [])
            results.extend(page_results)
            if len(page_results) < take:
                break
        return results

    def normalize(self, raw: dict, spec: SearchSpec) -> PostingDTO:
        salary = normalise_salary(raw.get("minimumSalary"), raw.get("maximumSalary"),
                                  currency=raw.get("currency") or "GBP")
        return PostingDTO(
            company_name=raw.get("employerName", "") or "Unknown (reed)",
            source=self.name,
            external_id=str(raw.get("jobId", "")),
            title=raw.get("jobTitle", ""),
            location=normalise_location(raw.get("locationName", "")),
            remote_policy="",
            department="",
            apply_url=raw.get("jobUrl", ""),
            description_text=strip_html(raw.get("jobDescription", "")),
            raw={**raw, "salary_normalised": salary, "posted_at": raw.get("date", ""),
                 "search": spec.label},
        )
