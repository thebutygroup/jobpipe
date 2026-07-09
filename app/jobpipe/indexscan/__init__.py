"""Weekly index scan: FTSE 100 + S&P 500 constituents -> careers resolution -> postings.

Pipeline per company: resolve careers URL and classify what powers it.
- Detectable ATS (Greenhouse/Lever/Ashby/Workable): auto-insert into the main
  registry; the daily ATS pollers take over. Zero new scraping surface.
- Workday: polled weekly via the semi-standard /wday/cxs JSON endpoint.
- Bespoke/other (SuccessFactors, Taleo, iCIMS, custom): FLAGGED for the v2
  agentic scout — deliberately NOT scraped. Blind HTML scraping of hundreds of
  bespoke portals fails silently and constantly; flagging is honest.
"""
