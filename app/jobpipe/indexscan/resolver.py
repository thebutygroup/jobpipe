"""Careers resolution: for one company, find the careers URL and classify it.

Domain discovery: from the static YAML if provided, else the company's
Wikipedia infobox website (one cached fetch, ever). Careers discovery: common
paths + homepage link scan. Classification: ATS token detection (shared with
the Built In poller), Workday tenant detection, known-bespoke markers.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..pollers.base import FetchError, polite_get
from ..pollers.builtin import detect_ats

log = logging.getLogger(__name__)

WORKDAY_RE = re.compile(
    r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com(?:/(?:[a-z]{2}-[A-Z]{2}/)?"
    r"([A-Za-z0-9_-]+))?", re.I)
BESPOKE_MARKERS = ("successfactors", "taleo", "icims", "oraclecloud", "eightfold",
                   "smartrecruiters", "jobvite", "phenom")
CAREER_PATHS = ("/careers", "/careers/", "/jobs", "/en/careers", "/about/careers",
                "/company/careers")
CAREER_LINK_RE = re.compile(r"career|jobs|join.?us|work.?with.?us|vacanc", re.I)


def detect_workday(text: str) -> dict:
    m = WORKDAY_RE.search(text or "")
    if not m:
        return {}
    tenant, wd, site = m.group(1), m.group(2), m.group(3)
    return {"workday_host": f"https://{tenant}.{wd}.myworkdayjobs.com",
            "workday_tenant": tenant, "workday_site": site or ""}


def classify_html(url: str, html: str) -> dict:
    """Classify a careers page (or any page) by its content + URL."""
    wd = detect_workday(url) or detect_workday(html)
    if wd:
        return {"status": "workday", **wd}
    ats = detect_ats(url)
    if ats:
        return {"status": "resolved_ats", **ats}
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        ats = detect_ats(a["href"])
        if ats:
            return {"status": "resolved_ats", **ats}
        wd = detect_workday(a["href"])
        if wd:
            return {"status": "workday", **wd}
    blob = (url + " " + html[:20000]).lower()
    if any(m in blob for m in BESPOKE_MARKERS):
        return {"status": "bespoke", "notes": "known enterprise portal; v2 agent scope"}
    return {"status": "bespoke", "notes": "no detectable ATS; v2 agent scope"}


def wikipedia_domain(company_name: str) -> str:
    """Company's website from its Wikipedia infobox 'Website' row ONLY.
    Ticker links (nasdaq.com, londonstockexchange.com, ...) must never win:
    they appear above the Website row in most infoboxes."""
    try:
        html = polite_get("https://en.wikipedia.org/wiki/"
                          + quote(company_name.replace(" ", "_")), max_retries=1).text
    except FetchError:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    box = soup.select_one("table.infobox")
    if not box:
        return ""
    for tr in box.find_all("tr"):
        th = tr.find("th")
        if th is None or "website" not in th.get_text(" ", strip=True).lower():
            continue
        for a in tr.find_all("a", href=True):
            href = a["href"]
            if href.startswith("http") and "wikipedia" not in href and "wikimedia" not in href:
                return urlsplit(href).netloc.removeprefix("www.")
    return ""


_failed_urls: set[str] = set()   # per-process memo: don't re-probe known-dead URLs


def _probe(url: str):
    """polite_get with 1 retry for speculative probes, memoised on failure."""
    if url in _failed_urls:
        raise FetchError(f"previously failed: {url}")
    try:
        return polite_get(url, max_retries=1)
    except FetchError:
        _failed_urls.add(url)
        raise


def find_careers_page(domain: str) -> tuple[str, str]:
    """Return (careers_url, html). Tries common paths, then homepage link scan."""
    base = f"https://{domain}"
    for path in CAREER_PATHS:
        try:
            resp = _probe(base + path)
            return str(resp.url), resp.text
        except FetchError:
            continue
    try:
        resp = _probe(base)
    except FetchError:
        return "", ""
    soup = BeautifulSoup(resp.text, "html.parser")
    for a in soup.find_all("a", href=True):
        if CAREER_LINK_RE.search(a.get_text(" ", strip=True)) or \
                CAREER_LINK_RE.search(a["href"]):
            url = urljoin(str(resp.url), a["href"])
            try:
                r2 = _probe(url)
                return str(r2.url), r2.text
            except FetchError:
                return url, ""
    return str(resp.url), resp.text  # classify the homepage itself as last resort


def domain_from_source_url(source_url: str) -> str:
    """Website hint: fetch a board's company page (e.g. Built In) and pull the
    company's own website link from it. Returns bare domain or ''."""
    from ..pollers.builtin import extract_company_website
    try:
        html = polite_get(source_url).text
    except FetchError:
        return ""
    website = extract_company_website(html)
    if not website:
        return ""
    return urlsplit(website).netloc.removeprefix("www.")


def resolve_company(name: str, domain: str = "", source_url: str = "",
                    careers_url: str = "") -> dict:
    """Full resolution for one company. Returns fields for index_companies.
    A manually supplied careers_url (reviewed spreadsheet) skips discovery
    entirely: we fetch that exact page and classify what powers it."""
    if careers_url:
        try:
            resp = polite_get(careers_url, max_retries=1)
        except FetchError:
            return {"status": "unresolved", "careers_url": careers_url,
                    "notes": "manual careers URL unreachable"}
        result = classify_html(str(resp.url), resp.text)
        result.update({"domain": urlsplit(careers_url).netloc.removeprefix("www."),
                       "careers_url": str(resp.url)})
        return result
    if not domain and source_url:
        domain = domain_from_source_url(source_url)
    domain = domain or wikipedia_domain(name)
    if not domain:
        return {"status": "unresolved", "notes": "no domain found"}
    careers_url, html = find_careers_page(domain)
    if not careers_url:
        return {"status": "unresolved", "domain": domain, "notes": "careers page not found"}
    result = classify_html(careers_url, html)
    result.update({"domain": domain, "careers_url": careers_url})
    return result
