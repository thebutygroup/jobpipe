"""Source registry: the single list of everywhere jobpipe ingests jobs from.

Adding source N+1: write an adapter (sources/<name>.py), register it here,
and — for aggregators — add a searches.yaml entry. Nothing else.
"""

from __future__ import annotations

from .adzuna import AdzunaAdapter
from .base import SourceAdapter
from .boards import ATS_ADAPTERS, BuiltinAdapter
from .reed import ReedAdapter

_REGISTRY: dict[str, SourceAdapter] = {}


def register(adapter: SourceAdapter) -> SourceAdapter:
    if not adapter.name:
        raise ValueError("adapter must set .name")
    _REGISTRY[adapter.name] = adapter
    return adapter


def get(name: str) -> SourceAdapter:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown source {name!r}; registered: {sorted(_REGISTRY)}") from None


def all_sources() -> dict[str, SourceAdapter]:
    return dict(_REGISTRY)


def aggregators() -> dict[str, SourceAdapter]:
    return {n: a for n, a in _REGISTRY.items() if a.kind == "aggregator"}


# ---- default registry ---------------------------------------------------------------
for _adapter in ATS_ADAPTERS.values():
    register(_adapter)
register(BuiltinAdapter())
register(AdzunaAdapter())
register(ReedAdapter())

ATS_NAMES = tuple(ATS_ADAPTERS)


def health(conn) -> list[dict]:
    """Per-source operational status, derived from events — queryable truth,
    no counters to drift. One row per registered source."""
    rows = conn.execute(
        "SELECT json_extract(payload_json, '$.source') AS source, "
        "       MAX(created_at) AS last_polled, "
        "       SUM(json_extract(payload_json, '$.new')) AS total_new, "
        "       SUM(json_extract(payload_json, '$.errors')) AS total_errors "
        "FROM events WHERE event_type = 'source_polled' GROUP BY source").fetchall()
    by_source = {r["source"]: r for r in rows}
    out = []
    for name, adapter in sorted(_REGISTRY.items()):
        seen = by_source.get(name)
        configured = adapter.is_configured()
        out.append({
            "source": name,
            "kind": adapter.kind,
            "status": "ok" if (configured and seen) else
                      ("unconfigured" if not configured else "never polled"),
            "reason": "" if configured else adapter.unconfigured_reason(),
            "last_polled": seen["last_polled"] if seen else None,
            "total_new": (seen["total_new"] or 0) if seen else 0,
            "total_errors": (seen["total_errors"] or 0) if seen else 0,
        })
    return out
