"""Board-source adapters folding the existing pollers into the adapter
contract. These reuse the poller modules' internals (HTTP + normalise) —
re-shaping, not a rewrite.
"""

from __future__ import annotations

from ..config import settings
from ..models import PostingDTO
from ..pollers import ashby, builtin, greenhouse, lever, workable
from .base import SearchSpec, SourceAdapter


class AtsBoardAdapter(SourceAdapter):
    """Per-company ATS board (Greenhouse/Lever/Ashby/Workable). spec carries
    company_name + board_token; postings.source stays 'ats' (matcher,
    crosslink and the ATS-preferred dedupe rule all key on it)."""

    kind = "ats-board"

    def __init__(self, name: str, module):
        self.name = name
        self.module = module

    def fetch(self, spec: SearchSpec) -> list[dict]:
        return self.module.fetch_raw(spec.board_token)

    def normalize(self, raw: dict, spec: SearchSpec) -> PostingDTO:
        dto = self.module.normalise(spec.company_name, raw)
        dto.source_detail = self.name
        return dto


class BuiltinAdapter(SourceAdapter):
    """Built In saved-search scrape. The HTML parser produces DTOs directly,
    so this overrides fetch_postings (detail resolution/company discovery
    stays in pollers.runner, where it needs DB access)."""

    name = "builtin"
    kind = "board-scrape"

    def fetch_postings(self, spec: SearchSpec) -> list[PostingDTO]:
        dtos = builtin.fetch_search(spec.url, max_pages=settings.builtin_max_pages)
        for dto in dtos:
            dto.source_detail = self.name
        return dtos


ATS_ADAPTERS = {
    "greenhouse": AtsBoardAdapter("greenhouse", greenhouse),
    "lever": AtsBoardAdapter("lever", lever),
    "ashby": AtsBoardAdapter("ashby", ashby),
    "workable": AtsBoardAdapter("workable", workable),
}
