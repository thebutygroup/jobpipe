"""Greenhouse applier — the MVP target platform.

Live finding (24 Jul, Anthropic posting): the NEW Greenhouse board UI
(job-boards.greenhouse.io) is a React SPA — static HTML extraction finds
nothing. The classic boards.greenhouse.io is server-rendered. Rather than
special-casing markup, extraction here uses Greenhouse's public Job Board
API, which returns the application form's QUESTIONS as structured JSON:

    GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true

(no auth required for GET; the company key is only needed to POST, which
third parties can't do — see docs/APPLY-FLOW-PLAN.md). This is strictly
better than scraping: exact field names, required flags, and select options.
Falls back to static HTML extraction for embedded/self-hosted variants.
"""

from __future__ import annotations

import logging
import re

from ...prepare.forms import FormField
from .base import PlatformApplier, register

log = logging.getLogger(__name__)

BOARD_URL = re.compile(
    r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/([^/?#]+)/jobs/(\d+)")
API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}?questions=true"
API_EU = "https://boards-api.eu.greenhouse.io/v1/boards/{token}/jobs/{job_id}?questions=true"

_KIND_MAP = {
    "input_text": "text",
    "textarea": "textarea",
    "input_file": "file",
    "multi_value_single_select": "select",
    "multi_value_multi_select": "select",
}


def parse_board_url(url: str) -> tuple[str, str] | None:
    m = BOARD_URL.search(url or "")
    return (m.group(1), m.group(2)) if m else None


def questions_to_fields(payload: dict) -> list[FormField]:
    """Map the Job Board API's questions JSON to our FormField schema."""
    fields: list[FormField] = []
    for q in payload.get("questions", []) + payload.get("location_questions", []):
        specs = q.get("fields") or []
        if not specs:
            continue
        spec = specs[0]
        kind = _KIND_MAP.get(spec.get("type", ""), None)
        if kind is None:                      # input_hidden etc.
            continue
        fields.append(FormField(
            key=spec.get("name", ""),
            label=q.get("label", "") or spec.get("name", ""),
            kind=kind,
            required=bool(q.get("required")),
            options=[v.get("label", "") for v in (spec.get("values") or [])],
        ))
    return fields


class GreenhouseApplier(PlatformApplier):
    name = "greenhouse"
    needs_browser = False   # extraction via API; filling still uses the browser
    success_signals = PlatformApplier.success_signals + (
        "your application was submitted successfully",
    )

    def extract(self, final_url: str) -> list[FormField]:
        parsed = parse_board_url(final_url)
        if parsed:
            token, job_id = parsed
            api = API_EU if ".eu.greenhouse.io" in final_url else API
            try:
                from ...pollers.base import polite_get

                payload = polite_get(api.format(token=token, job_id=job_id)).json()
                fields = questions_to_fields(payload)
                if fields:
                    log.info("greenhouse: %d fields via Job Board API for %s/%s",
                             len(fields), token, job_id)
                    return fields
            except Exception:
                log.exception("greenhouse job-board API failed; falling back to HTML")
        return super().extract(final_url)


register(GreenhouseApplier())
