"""Shared HTTP client with politeness rules: rate limiting, honest UA,
backoff on 429/5xx."""

from __future__ import annotations

import logging
import time

import requests

from ..config import settings

log = logging.getLogger(__name__)
_last_request_at: dict[str, float] = {}


class FetchError(Exception):
    """HTTP fetch gave up. `status` carries the HTTP status when the failure
    was a definitive non-retryable response (404, 403, ...); None when the
    failure was network-level or retries were exhausted — callers use it to
    tell 'this URL is gone' from 'the internet hiccuped'."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def polite_get(url: str, *, timeout: int = 30, max_retries: int = 3,
               auth: tuple[str, str] | None = None,
               params: dict | None = None,
               cookies: dict | None = None) -> requests.Response:
    from urllib.parse import urlsplit

    host = urlsplit(url).netloc
    wait = settings.request_min_interval_s - (time.monotonic() - _last_request_at.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)

    backoff = 2.0
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        _last_request_at[host] = time.monotonic()
        try:
            resp = requests.get(url, timeout=timeout, auth=auth, params=params,
                                cookies=cookies,
                                headers={"User-Agent": settings.user_agent})
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504):
                log.warning("HTTP %s from %s (attempt %d)", resp.status_code, host, attempt)
                time.sleep(backoff)
                backoff *= 2
                continue
            raise FetchError(f"HTTP {resp.status_code} for {url}",
                             status=resp.status_code)
        except requests.RequestException as e:
            last_exc = e
            log.warning("request error for %s (attempt %d): %s", url, attempt, e)
            time.sleep(backoff)
            backoff *= 2
    raise FetchError(f"failed after {max_retries} attempts: {url}") from last_exc


def strip_html(html: str) -> str:
    from bs4 import BeautifulSoup

    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n", strip=True)
