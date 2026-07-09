"""Pluggable CAPTCHA resolvers.

- human (default): pause in NEEDS_HUMAN, email Joe. Solving happens at the
  machine / via remote desktop; a noVNC sidecar is
  the planned upgrade. The paused application waits indefinitely.
- service (opt-in, CAPTCHA_RESOLVER=service): 2Captcha token solving for
  reCAPTCHA v2 / hCaptcha ONLY. Behavioural challenges (reCAPTCHA v3/Enterprise,
  Turnstile) always fall through to human — token services cannot fix a low
  behaviour score, and injected tokens risk silent rejection.
"""

from __future__ import annotations

import logging
import time

import requests

from ..config import settings

log = logging.getLogger(__name__)
TOKEN_SOLVABLE = ("recaptcha", "g-recaptcha", "hcaptcha", "h-captcha")
BEHAVIOURAL = ("turnstile", "cf-turnstile")


def resolver_for(blocker: str):
    if settings.captcha_resolver == "service" and _token_solvable(blocker):
        return solve_with_service
    return human_pause_resolver


def _token_solvable(blocker: str) -> bool:
    b = blocker.lower()
    return any(t in b for t in TOKEN_SOLVABLE) and not any(t in b for t in BEHAVIOURAL)


def human_pause_resolver(page, blocker: str) -> bool:
    """Never solves in-process; the runner parks the app in NEEDS_HUMAN."""
    return False


def solve_with_service(page, blocker: str, poll_s: int = 5, timeout_s: int = 180) -> bool:
    """2Captcha reCAPTCHA v2 flow: submit sitekey+url, poll, inject token."""
    api_key = settings.twocaptcha_api_key
    if not api_key:
        log.warning("CAPTCHA_RESOLVER=service but no TWOCAPTCHA_API_KEY; falling to human")
        return False
    sitekey_el = page.locator("[data-sitekey]").first
    if sitekey_el.count() == 0:
        return False
    sitekey = sitekey_el.get_attribute("data-sitekey")
    submit = requests.post("https://2captcha.com/in.php", data={
        "key": api_key, "method": "userrecaptcha", "googlekey": sitekey,
        "pageurl": page.url, "json": 1}, timeout=30).json()
    if submit.get("status") != 1:
        log.warning("2captcha submit failed: %s", submit)
        return False
    task_id = submit["request"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(poll_s)
        res = requests.get("https://2captcha.com/res.php", params={
            "key": api_key, "action": "get", "id": task_id, "json": 1}, timeout=30).json()
        if res.get("status") == 1:
            token = res["request"]
            page.evaluate(
                "(t) => { const el = document.getElementById('g-recaptcha-response');"
                " if (el) { el.style.display=''; el.value = t; } }", token)
            return True
        if res.get("request") != "CAPCHA_NOT_READY":
            log.warning("2captcha error: %s", res)
            return False
    return False
