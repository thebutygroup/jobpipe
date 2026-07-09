"""Discover job-detail links on a careers page, and the capped LLM fallback
for pages that carry no JSON-LD.
"""
from __future__ import annotations

import json
import logging
import re
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..config import settings

log = logging.getLogger(__name__)

JOB_PATH_HINTS = re.compile(
    r"/(job|jobs|careers?|vacanc|position|opening|role)s?/", re.I)
NOISE = re.compile(r"/(privacy|cookie|login|about|blog|news|contact|benefits)", re.I)


def allowed_by_robots(url: str, fetch) -> bool:
    """Check robots.txt once per call; unreachable robots.txt => allow."""
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    rp = robotparser.RobotFileParser()
    try:
        rp.parse(fetch(robots_url).text.splitlines())
    except Exception:
        return True
    return rp.can_fetch("jobpipe", url)


def find_job_links(careers_url: str, html: str, cap: int) -> list[str]:
    """Same-domain links that look like individual job pages."""
    soup = BeautifulSoup(html, "html.parser")
    domain = urlsplit(careers_url).netloc
    seen: dict[str, None] = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(careers_url, a["href"]).split("#")[0]
        parts = urlsplit(url)
        if parts.netloc != domain:
            continue
        if not JOB_PATH_HINTS.search(parts.path) or NOISE.search(parts.path):
            continue
        if url.rstrip("/") == careers_url.rstrip("/"):
            continue
        seen[url] = None
        if len(seen) >= cap:
            break
    return list(seen)


LLM_EXTRACT_PROMPT = """Extract the job posting from this careers web page text.
Respond with ONLY a JSON object, no markdown fences, no prose:
{{"is_job_page": <true|false>, "title": "<str>", "location": "<str>",
 "description": "<full posting text, plain>", "apply_url": "<str or empty>"}}
If the page is a listing/index or not a job posting, set is_job_page to false.

PAGE URL: {url}

PAGE TEXT:
{text}
"""


def llm_extract(client, url: str, html: str) -> dict | None:
    """Capped Haiku fallback: page -> structured job dict (or None)."""
    text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)[:12000]
    resp = client.messages.create(
        model=settings.match_model,
        max_tokens=2000,
        temperature=0,
        messages=[{"role": "user",
                   "content": LLM_EXTRACT_PROMPT.format(url=url, text=text)}],
    )
    raw = resp.content[0].text.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("llm_extract returned non-JSON for %s", url)
        return None
    if not data.get("is_job_page") or not data.get("title"):
        return None
    return {
        "title": data["title"],
        "location": data.get("location", ""),
        "description": data.get("description", ""),
        "apply_url": data.get("apply_url") or url,
        "employment_type": "",
        "date_posted": "",
    }
