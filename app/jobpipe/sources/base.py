"""The source adapter contract.

Every source implements:
    fetch(spec)     -> list[dict]        raw payload items, exactly as the API
                                         returned them (recorded for provenance)
    normalize(raw, spec) -> PostingDTO   unified schema

Sources that need credentials report is_configured() = False until their env
vars are set; the runner then logs one clear line and skips them ("no-op
without keys"). Nothing raises just because a key is missing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..models import PostingDTO


@dataclass
class SearchSpec:
    """One unit of polling work.

    For aggregator sources: keywords + location (location is a per-search
    parameter — London is the default in the shipped searches.yaml, not a
    hardcode). For board sources: company_name + board_token. For Built In:
    url.
    """

    source: str
    name: str = ""
    # aggregator params
    keywords: str = ""
    location: str = "London"
    distance_miles: int = 15
    # ATS board params
    company_name: str = ""
    board_token: str = ""
    # builtin params
    url: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.name or self.keywords or self.company_name or self.url or self.source


class SourceAdapter(ABC):
    """One ingestion source. Stateless; credentials come from settings.

    API-based adapters implement fetch() + normalize() (so normalization is
    unit-testable against recorded fixture payloads). Scrape sources whose
    parser produces DTOs directly may override fetch_postings() instead.
    """

    name: str = ""            # registry key; also the provenance source tag
    kind: str = "aggregator"  # 'ats-board' | 'board-scrape' | 'aggregator'

    def is_configured(self) -> bool:
        """False => the runner skips this source with a clear log line and
        marks it 'unconfigured' in health output. Keyless sources return True."""
        return True

    def unconfigured_reason(self) -> str:
        return ""

    def fetch(self, spec: SearchSpec) -> list[dict]:
        """Fetch raw payload items for one search/board. May raise FetchError —
        the runner isolates failures per source."""
        raise NotImplementedError(f"{self.name}: fetch() not implemented")

    def normalize(self, raw: dict, spec: SearchSpec) -> PostingDTO:
        """Map one raw item into the unified PostingDTO schema."""
        raise NotImplementedError(f"{self.name}: normalize() not implemented")

    def fetch_postings(self, spec: SearchSpec) -> list[PostingDTO]:
        """The runner's entry point: fetch + normalize, tagged with this
        adapter's name for provenance."""
        out = []
        for raw in self.fetch(spec):
            dto = self.normalize(raw, spec)
            dto.source_detail = dto.source_detail or self.name
            out.append(dto)
        return out


# ---- salary / location normalisation helpers (shared by aggregators) -----------------

def normalise_salary(min_v, max_v, currency: str = "GBP", period: str = "year") -> dict:
    """Unified salary dict stored in DTO.raw['salary_normalised']."""
    try:
        lo = int(float(min_v)) if min_v else None
        hi = int(float(max_v)) if max_v else None
    except (TypeError, ValueError):
        lo, hi = None, None
    if lo is not None and hi is not None and hi < lo:
        lo, hi = hi, lo
    return {"min": lo, "max": hi, "currency": currency or "GBP", "period": period or "year"}


def normalise_location(display: str) -> str:
    """Trim aggregator location strings to a stable 'City, Region' form."""
    parts = [p.strip() for p in (display or "").split(",") if p.strip()]
    return ", ".join(parts[:2])
