"""Source adapters: every place jobpipe ingests jobs from, behind one contract.

A source is either:
- a *board* source (per-company ATS boards: greenhouse/lever/ashby/workable,
  plus the Built In scrape) — driven by the companies registry / saved searches
- an *aggregator* source (Adzuna, Reed) — driven by keyword searches with an
  explicit location parameter

Adding source N+1 = one adapter class + one register() call + (for
aggregators) a searches.yaml entry. See base.SourceAdapter.
"""

from . import registry  # noqa: F401  (import wires up the default registry)
