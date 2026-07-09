"""Extract schema.org JobPosting objects from a page's JSON-LD blocks.

Handles the shapes seen in the wild: a bare JobPosting, a list of them,
@graph containers, and ItemList wrappers. Tolerates malformed JSON in one
block without losing the others.
"""
from __future__ import annotations

import json
import logging

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


def _walk(node) -> list[dict]:
    """Recursively collect dicts whose @type is (or includes) JobPosting."""
    found: list[dict] = []
    if isinstance(node, dict):
        t = node.get("@type", "")
        types = t if isinstance(t, list) else [t]
        if "JobPosting" in types:
            found.append(node)
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in node:
                found.extend(_walk(node[key]))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk(item))
    return found


def _text(value) -> str:
    """JSON-LD values may be strings, dicts ({'@value': ...}) or lists."""
    if isinstance(value, dict):
        return str(value.get("@value") or value.get("name") or "")
    if isinstance(value, list):
        return ", ".join(_text(v) for v in value if v)
    return str(value or "")


def _location(jp: dict) -> str:
    loc = jp.get("jobLocation")
    locs = loc if isinstance(loc, list) else [loc]
    parts = []
    for entry in locs:
        if not isinstance(entry, dict):
            continue
        addr = entry.get("address", {})
        if isinstance(addr, dict):
            piece = ", ".join(
                str(addr.get(k)) for k in ("addressLocality", "addressRegion", "addressCountry")
                if addr.get(k))
            if piece:
                parts.append(piece)
    if jp.get("jobLocationType") == "TELECOMMUTE":
        parts.append("Remote")
    return "; ".join(dict.fromkeys(parts))  # dedupe, keep order


def _description(jp: dict) -> str:
    html = _text(jp.get("description"))
    if "<" in html:  # description is commonly HTML
        soup = BeautifulSoup(html, "html.parser")
        # paragraph breaks between block elements, plain spaces inside them
        for block in soup.find_all(["p", "li", "br", "div", "h2", "h3"]):
            block.append("\n")
        return " ".join(soup.get_text(" ", strip=True).split(" \n ")).replace(" \n", "\n").strip()
    return html


def extract_jobpostings(html: str, page_url: str = "") -> list[dict]:
    """Return normalised job dicts from all JSON-LD blocks on a page."""
    soup = BeautifulSoup(html, "html.parser")
    raw: list[dict] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        raw.extend(_walk(data))

    jobs = []
    for jp in raw:
        title = _text(jp.get("title"))
        if not title:
            continue
        jobs.append({
            "title": title,
            "location": _location(jp),
            "description": _description(jp),
            "apply_url": _text(jp.get("url")) or page_url,
            "employment_type": _text(jp.get("employmentType")),
            "date_posted": _text(jp.get("datePosted")),
        })
    return jobs
