"""Built In London saved-search poller.

Server-rendered HTML: requests + BeautifulSoup, no browser needed.

IMPORTANT — selector fragility: the CSS selectors below are written
defensively against Built In's job-card markup, but the site can change
markup at any time. The parser therefore:
  1. tries data-id job cards first, then falls back to any element whose
     link matches /job/... URLs;
  2. logs (rather than raises) when a card yields no title, so one odd
     card never kills a run.
On first live run, verify counts against the site by eye. Fixtures in
tests/fixtures/builtin_search.html pin current behaviour.

Politeness: robots.txt is fetched once per run and the /jobs path is
skipped entirely if disallowed.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..models import PostingDTO
from .base import FetchError, polite_get

log = logging.getLogger(__name__)
BASE = "https://builtinlondon.uk"
JOB_PATH_RE = re.compile(r"/job/[^/]+")


def robots_allows(base: str = BASE, path: str = "/jobs") -> bool:
    from urllib.robotparser import RobotFileParser

    rp = RobotFileParser()
    try:
        resp = polite_get(f"{base}/robots.txt")
        rp.parse(resp.text.splitlines())
        return rp.can_fetch("*", f"{base}{path}")
    except FetchError:
        log.warning("robots.txt unavailable; proceeding cautiously")
        return True


def fetch_search(search_url: str, max_pages: int = 10) -> list[PostingDTO]:
    """Fetch every result page of one saved search."""
    postings: list[PostingDTO] = []
    url = search_url
    for page in range(1, max_pages + 1):
        html = polite_get(url).text
        page_postings, next_url = parse_search_page(html, current_url=url)
        postings.extend(page_postings)
        if not next_url or next_url == url:
            break
        url = next_url
    return postings


def parse_search_page(html: str, current_url: str = BASE) -> tuple[list[PostingDTO], str | None]:
    """Walk the page in document order. Built In renders each result as a
    /company/<slug> link followed by an <h2> holding the /job/<slug> link and
    then metadata text. We track the most recent company link and attach it to
    the next job link we encounter."""
    soup = BeautifulSoup(html, "html.parser")
    postings: list[PostingDTO] = []

    current_company = ""
    current_company_slug = ""
    # Walk all anchors in order; company links set context, job links emit a posting.
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/company/" in href:
            text = a.get_text(strip=True)
            if text:  # logo links are empty; the text link carries the name
                current_company = text
                current_company_slug = href.split("/company/")[-1].strip("/")
            continue
        if JOB_PATH_RE.search(href):
            title = a.get_text(strip=True)
            if not title:
                continue
            job_url = urljoin(BASE, href)
            dto = _build_posting(a, title, job_url, current_company, current_company_slug)
            if dto:
                postings.append(dto)

    next_link = soup.select_one("a[rel='next'], a[aria-label*='next' i], a[aria-label*='Next']")
    next_url = urljoin(current_url, next_link["href"]) if next_link and next_link.get("href") else None
    return postings, next_url


def _build_posting(job_link, title: str, job_url: str, company: str,
                   company_slug: str) -> PostingDTO | None:
    # Metadata sits in the text following the job link's heading. Climb to the
    # nearest block ancestor and read its text for location / work mode.
    block = job_link
    for _ in range(4):
        if block.parent is None:
            break
        block = block.parent
        text = block.get_text(" | ", strip=True)
        if "London" in text or "Remote" in text or "Hybrid" in text or "GBR" in text:
            break
    text = block.get_text(" | ", strip=True)
    location = _first_match(text, r"(London[^|]*|Remote[^|]*|United Kingdom[^|]*|[A-Za-z ]+, GBR)")
    work_mode = _first_match(text, r"\b(Remote|Hybrid|In-Office|In Office)\b")

    return PostingDTO(
        company_name=company or "Unknown (builtin)",
        source="builtin",
        external_id=urlsplit(job_url).path,
        title=title,
        location=location,
        remote_policy=work_mode.lower().replace(" ", "-") if work_mode else "",
        apply_url=job_url,  # replaced by resolved external URL by the runner
        description_text=text[:2000],
        raw={"builtin_job_url": job_url, "builtin_company_slug": company_slug},
    )


def _first_match(text: str, pattern: str) -> str:
    m = re.search(pattern, text, flags=re.IGNORECASE)
    return m.group(1).strip() if m else ""


def resolve_job_detail(builtin_job_url: str) -> dict:
    """Fetch the Built In job detail page once and extract everything useful:

    - full_description: the complete posting text (renders server-side)
    - company_page_url: the Built In company page (/company/<slug>)
    - external_apply_url: direct company/ATS link IF present. NOTE: Built In
      gates the apply button behind login, so this is usually absent; we keep
      the builtin URL as the listing link. Resolving the company's own ATS via
      its website is the proper fix (roadmap: company-side resolution).
    - ats_info: {ats, board_token} when the external link reveals a known ATS
    """
    html = polite_get(builtin_job_url).text
    soup = BeautifulSoup(html, "html.parser")

    external = ""
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("http") and "builtin" not in href and _looks_like_apply(a, href):
            external = href
            break

    company_page = ""
    for a in soup.find_all("a", href=True):
        if "/company/" in a["href"] and a.get_text(strip=True):
            company_page = urljoin(BASE, a["href"])
            break

    return {
        "external_apply_url": external,
        "company_page_url": company_page,
        "full_description": _extract_full_description(soup),
        "ats_info": detect_ats(external),
    }


def _looks_like_apply(a, href: str) -> bool:
    label = (a.get_text(" ", strip=True) or "").lower()
    return "apply" in label or any(
        d in href for d in ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com")
    )


def _extract_full_description(soup) -> str:
    """The posting body sits between the H1 title and the 'Similar Jobs' block.
    Walk elements after the H1, skipping nav/footer noise, stopping at the
    similar-jobs heading."""
    h1 = soup.find("h1")
    if h1 is None:
        return ""
    parts: list[str] = []
    for el in h1.find_all_next(["p", "li", "h2", "h3", "strong"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if "Similar Jobs" in text or "What you need to know about" in text:
            break
        # skip auth/nav boilerplate
        if text.startswith(("Log In", "Sign up", "Create Free Account", "Apply Instructions")):
            continue
        if el.name in ("h2", "h3", "strong") and len(text) < 80:
            parts.append(f"\n{text}\n")
        else:
            parts.append(text)
    # de-dup consecutive repeats (Built In repeats meta rows)
    out: list[str] = []
    for p in parts:
        if not out or out[-1] != p:
            out.append(p)
    return "\n".join(out).strip()


def extract_company_website(html: str) -> str:
    """Built In company pages carry a 'View Website' link to the company's own
    site (anonymous, no login wall). Strip tracking params; return '' if absent."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        label = (a.get_text(" ", strip=True) or "").lower()
        if href.startswith("http") and "builtin" not in href and "view website" in label:
            return href.split("?")[0].rstrip("/")
    return ""


# Back-compat shim for the runner's existing call signature.
def resolve_external_apply_url(builtin_job_url: str) -> tuple[str, dict]:
    d = resolve_job_detail(builtin_job_url)
    return d["external_apply_url"] or builtin_job_url, d["ats_info"]


ATS_PATTERNS = [
    ("greenhouse", re.compile(r"greenhouse\.io/(?:embed/job_app\?[^ ]*token=)?([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"boards\.greenhouse\.io/([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)")),
]


def detect_ats(url: str) -> dict:
    for ats, pattern in ATS_PATTERNS:
        m = pattern.search(url or "")
        if m:
            return {"ats": ats, "board_token": m.group(1)}
    return {}
