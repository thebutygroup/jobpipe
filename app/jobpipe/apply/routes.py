"""Route resolution: from a posting's listing URL to the place where the
application actually happens.

Research finding (docs/APPLY-FLOW-PLAN.md): aggregators (Adzuna) and Built In
are ROUTERS — the apply flow always terminates on an ATS form, a company
career site, or a login-walled board. This module follows the chain and
classifies the landing platform. Every resolution records its hop chain for
audit and is persisted (one route per posting, re-resolvable).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

BROWSER_FORM = "browser_form"
MANUAL_ASSIST = "manual_assist"

# Terminal platforms and their URL signatures. Order matters (first match wins).
_ATS_SIGNATURES = [
    ("greenhouse", ("boards.greenhouse.io", "job-boards.greenhouse.io",
                    "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io")),
    ("lever", ("jobs.lever.co", "jobs.eu.lever.co")),
    ("ashby", ("jobs.ashbyhq.com",)),
    ("workable", ("apply.workable.com",)),
]
# Login-walled boards: applying requires an account there -> manual assist.
_LOGIN_WALLED = ("reed.co.uk", "linkedin.com", "indeed.com", "glassdoor.")
# Routers: never a destination; must be followed further.
_ROUTER_BUILTIN = ("builtinlondon.uk", "builtin.com")
_ROUTER_ADZUNA = ("adzuna.co.uk", "adzuna.com")

MAX_HOPS = 5


@dataclass
class ApplyRoute:
    platform: str            # greenhouse|lever|ashby|workable|company_site|login_walled
    method: str              # browser_form | manual_assist
    final_url: str
    hops: list[str] = field(default_factory=list)
    notes: str = ""


def classify(url: str) -> str:
    """Pure classification of a URL: terminal platform, router, or unknown."""
    host = urlsplit(url or "").netloc.lower()
    if not host:
        return "invalid"
    for platform, hosts in _ATS_SIGNATURES:
        if any(host == h or host.endswith("." + h) for h in hosts):
            return platform
    if any(marker in host for marker in _LOGIN_WALLED):
        return "login_walled"
    if any(host == h or host.endswith("." + h) for h in _ROUTER_BUILTIN):
        return "router:builtin"
    if any(host == h or host.endswith("." + h) for h in _ROUTER_ADZUNA):
        return "router:adzuna"
    return "company_site"


def _default_follow(url: str) -> str:
    """Follow HTTP redirects and return the landing URL (network)."""
    from ..pollers.base import polite_get

    return polite_get(url).url


def _default_builtin_resolve(url: str) -> str:
    """Resolve a Built In job page to its external apply URL (network)."""
    from ..pollers import builtin

    detail = builtin.resolve_job_detail(url)
    return detail.get("external_apply_url") or ""


def resolve_route(start_url: str, *, follow=_default_follow,
                  builtin_resolve=_default_builtin_resolve) -> ApplyRoute:
    """Walk from a listing URL to the application destination.

    `follow` and `builtin_resolve` are injectable for tests — the logic is
    fully exercisable without network.
    """
    hops = [start_url]
    url = start_url
    for _ in range(MAX_HOPS):
        kind = classify(url)
        if kind == "invalid":
            return ApplyRoute("company_site", MANUAL_ASSIST, url, hops,
                              notes="unresolvable url")
        if kind == "login_walled":
            return ApplyRoute("login_walled", MANUAL_ASSIST, url, hops,
                              notes="account required on the board")
        if kind == "router:builtin":
            nxt = builtin_resolve(url)
            if not nxt:
                return ApplyRoute("login_walled", MANUAL_ASSIST, url, hops,
                                  notes="builtin detail unresolvable (login wall?)")
            hops.append(nxt)
            url = nxt
            continue
        if kind == "router:adzuna":
            nxt = follow(url)
            if not nxt or nxt == url:
                return ApplyRoute("company_site", MANUAL_ASSIST, url, hops,
                                  notes="adzuna redirect did not resolve")
            hops.append(nxt)
            url = nxt
            continue
        # terminal: a known ATS or an unknown company site
        return ApplyRoute(kind, BROWSER_FORM, url, hops)
    return ApplyRoute("company_site", MANUAL_ASSIST, url, hops,
                      notes=f"gave up after {MAX_HOPS} hops")


# ---- persistence ---------------------------------------------------------------------

def posting_start_url(posting_row) -> str:
    """Best starting URL for a posting row (canonical > apply > builtin raw)."""
    raw = json.loads(posting_row["raw_json"] or "{}") if "raw_json" in posting_row.keys() else {}
    return (posting_row["canonical_apply_url"] or posting_row["apply_url"]
            or raw.get("builtin_job_url") or "")


def save_route(conn, posting_id: int, route: ApplyRoute) -> None:
    from ..db import now, tx

    with tx(conn):
        conn.execute(
            "INSERT INTO apply_routes (posting_id, platform, method, final_url,"
            " hops_json, resolved_at, notes) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(posting_id) DO UPDATE SET platform=excluded.platform,"
            " method=excluded.method, final_url=excluded.final_url,"
            " hops_json=excluded.hops_json, resolved_at=excluded.resolved_at,"
            " notes=excluded.notes",
            (posting_id, route.platform, route.method, route.final_url,
             json.dumps(route.hops), now(), route.notes))


def load_route(conn, posting_id: int) -> ApplyRoute | None:
    row = conn.execute("SELECT * FROM apply_routes WHERE posting_id = ?",
                       (posting_id,)).fetchone()
    if not row:
        return None
    return ApplyRoute(row["platform"], row["method"], row["final_url"],
                      json.loads(row["hops_json"] or "[]"), row["notes"] or "")


def ensure_route(conn, posting_id: int, **resolver_kw) -> ApplyRoute:
    """Load the cached route or resolve + persist it."""
    route = load_route(conn, posting_id)
    if route:
        return route
    row = conn.execute("SELECT * FROM postings WHERE id = ?", (posting_id,)).fetchone()
    if row is None:
        raise ValueError(f"no posting {posting_id}")
    start = posting_start_url(row)
    route = resolve_route(start, **resolver_kw) if start else ApplyRoute(
        "company_site", MANUAL_ASSIST, "", [], notes="posting has no URL")
    save_route(conn, posting_id, route)
    return route
