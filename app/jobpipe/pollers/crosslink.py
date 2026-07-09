"""Cross-source linking: upgrade Built In postings to their company-site twins.

Once the company resolver has added a company's own ATS board and the daily
pollers have ingested it, the same job exists twice: the Built In posting
(board apply URL, behind a login wall) and the ATS posting (real company
apply URL). This step matches them by normalised title within a company and:

- stamps the Built In posting's canonical_apply_url with the company URL,
  so every page linking to that posting now sends you to the company site
- sets duplicate_of so the matcher skips the Built In twin (no double LLM
  spend, no duplicate rows on the matches page)

The ATS posting is canonical going forward; existing matches/applications on
the Built In posting stay intact and simply gain the better link.
"""
from __future__ import annotations

import logging
import re

from ..db import log_event, tx

log = logging.getLogger(__name__)

_norm_re = re.compile(r"[^a-z0-9 ]+")


def normalise_title(title: str) -> str:
    t = _norm_re.sub(" ", (title or "").lower())
    return " ".join(t.split())


def link_cross_source(conn) -> int:
    """For companies that have both builtin and ats postings, link twins."""
    rows = conn.execute(
        "SELECT b.id AS b_id, b.title AS b_title, a.id AS a_id, a.title AS a_title, "
        "       a.apply_url AS a_url "
        "FROM postings b "
        "JOIN postings a ON a.company_id = b.company_id AND a.source = 'ats' "
        "WHERE b.source = 'builtin' AND b.duplicate_of IS NULL "
        "AND b.closed_at IS NULL AND a.closed_at IS NULL"
    ).fetchall()
    linked = 0
    with tx(conn):
        for r in rows:
            if normalise_title(r["b_title"]) == normalise_title(r["a_title"]):
                conn.execute(
                    "UPDATE postings SET duplicate_of = ?, canonical_apply_url = ? WHERE id = ?",
                    (r["a_id"], r["a_url"], r["b_id"]))
                log_event(conn, "crosslink", posting_id=r["b_id"],
                          payload={"twin": r["a_id"], "company_url": r["a_url"]})
                linked += 1
    if linked:
        log.info("cross-linked %s builtin postings to company-site twins", linked)
    return linked
