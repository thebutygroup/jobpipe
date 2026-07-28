"""Confirmation banners for the internal review actions.

There is no session framework here (see settings.py — no ORM, no sessions), so
the "approved ✓" you expect after clicking Approve rides back on a querystring
key and is rendered by base.html.

Two deliberate constraints:

* Only slugs in MESSAGES render. The banner never echoes text from the URL —
  otherwise any link could paint an arbitrary sentence in jobpipe's own chrome,
  which is a phishing surface, not a convenience.
* Only the PATH of the Referer is honoured, and only when it names a page an
  action can sensibly return to. A crafted Referer therefore cannot bounce a
  reviewer off-site.
"""

import re
from urllib.parse import urlsplit

from django.shortcuts import redirect

MESSAGES = {
    "approved": "approved ✓",
    "rejected": "rejected ✓",
    "resumed": "resumed — handed back to the submitter ✓",
    "applied": "marked as applied ✓",
    "saved": "saved ✓",
}

# Pages an action may return to: the queue, the dense table (optionally scoped
# to a user) and a single application. Note the anchored `$` — a protocol-
# relative "//evil.com" arrives from urlsplit as a path and must not match.
_RETURNABLE = re.compile(r"^/(queue|all|all/[A-Za-z0-9_-]{1,30}|app/[0-9]+)$")


def back_to(request, slug: str, default: str):
    """Redirect to the page the action was fired from, carrying a banner.

    `default` is used whenever the Referer is missing, off-site or not a page
    worth returning to — so a POST made without a Referer still lands somewhere
    sensible rather than on the public landing page.
    """
    path = urlsplit(request.META.get("HTTP_REFERER", "")).path
    if not _RETURNABLE.match(path):
        path = default
    return redirect(f"{path}?done={slug}")


def banner(request):
    """Context processor: hands base.html the confirmation to render, if any."""
    return {"flash": MESSAGES.get(request.GET.get("done", ""))}
