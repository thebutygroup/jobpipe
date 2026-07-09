"""Careers-site crawler for companies NOT on a known ATS ("bespoke").

Targets (weekly, with the Sunday scan):
- index_companies rows with status='bespoke' (resolver found a careers page
  but no recognisable ATS behind it)
- the manual dream-company list (dream.yaml) after resolution

Strategy, deterministic-before-LLM:
1. Fetch the careers page (robots.txt respected, polite per-domain pacing)
2. schema.org JobPosting JSON-LD on the page itself (many sites embed it
   for Google Jobs) -> postings directly
3. Otherwise discover job-detail links (URL heuristics), fetch up to
   CRAWL_PAGE_CAP per domain, and read each page's JSON-LD
4. Pages with no JSON-LD fall back to capped Haiku extraction
   (CRAWL_LLM_CAP pages per run) returning strict JSON

Crawled postings are stored with source='ats' ("from the company's own
site"), so cross-source linking upgrades Built In twins automatically.

Guardrails: robots.txt, per-domain page cap, LLM cap, honest User-Agent,
public pages only, CRAWL_ENABLED master switch.
"""
