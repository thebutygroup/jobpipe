"""Index constituent lists: Wikipedia weekly refresh + bundled static fallback.

Wikipedia is the primary source (tables change as constituents change); the
static YAML keeps the scan alive when Wikipedia markup shifts or fetch fails.
Merge = union, Wikipedia rows preferred (they may carry fresher names).
"""

from __future__ import annotations

import logging

import yaml
from bs4 import BeautifulSoup

from ..pollers.base import FetchError, polite_get

log = logging.getLogger(__name__)

SOURCES = {
    "ftse100": "https://en.wikipedia.org/wiki/FTSE_100_Index",
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
}
# Column header the company name lives under, per page (matched loosely).
NAME_HEADERS = {"ftse100": ("company",), "sp500": ("security", "company")}


def parse_constituents(html: str, index: str) -> list[str]:
    """Find the constituents table by header match and pull the name column."""
    soup = BeautifulSoup(html, "html.parser")
    wanted = NAME_HEADERS[index]
    for table in soup.select("table.wikitable"):
        headers = [th.get_text(" ", strip=True).lower() for th in table.select("tr th")]
        col = next((i for i, h in enumerate(headers)
                    if any(w in h for w in wanted)), None)
        if col is None or len(table.select("tr")) < 50:  # constituents tables are big
            continue
        names = []
        for tr in table.select("tr")[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) > col:
                name = cells[col].get_text(" ", strip=True)
                if name:
                    names.append(name)
        if len(names) >= 50:
            return names
    return []


def fetch_index(index: str) -> list[str]:
    html = polite_get(SOURCES[index]).text
    return parse_constituents(html, index)


def load_static(path: str) -> dict[str, list[str]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return {k: v or [] for k, v in data.get("indexes", {}).items()}
    except FileNotFoundError:
        log.warning("static constituents file missing: %s", path)
        return {}


def merged_constituents(static_path: str) -> dict[str, list[str]]:
    """Wikipedia primary, static fallback; union with wiki order first."""
    static = load_static(static_path)
    out: dict[str, list[str]] = {}
    for index in SOURCES:
        wiki: list[str] = []
        try:
            wiki = fetch_index(index)
            if not wiki:
                log.warning("wikipedia parse yielded 0 rows for %s; markup drift? "
                            "falling back to static", index)
        except FetchError:
            log.warning("wikipedia fetch failed for %s; using static list", index)
        seen = {n.lower() for n in wiki}
        out[index] = wiki + [n for n in static.get(index, []) if n.lower() not in seen]
    return out
