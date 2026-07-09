"""Playwright form filler — runs ONLY in the submitter container.

Fill strictly from answers_json: the browser code decides WHERE values go
(selector resolution), never WHAT the values are. Verification re-reads every
field and diffs against intent before submit is allowed.
"""

from __future__ import annotations

import logging
import random
import time

log = logging.getLogger(__name__)

CAPTCHA_SELECTORS = [
    "iframe[src*='recaptcha']", ".g-recaptcha", "[data-sitekey]",
    "iframe[src*='hcaptcha']", ".h-captcha", ".cf-turnstile",
    "iframe[src*='turnstile']",
]
LOGIN_MARKERS = ["input[type='password']", "form[action*='login']", "form[action*='signin']"]
SUCCESS_PATTERNS = [
    "thank you for applying", "application received", "application submitted",
    "we have received your application", "thanks for applying",
    "your application has been submitted",
]


def human_pause(lo: float = 0.15, hi: float = 0.4) -> None:
    time.sleep(random.uniform(lo, hi))


def detect_blocker(page) -> str | None:
    for sel in CAPTCHA_SELECTORS:
        if page.locator(sel).count() > 0:
            return f"captcha:{sel}"
    for sel in LOGIN_MARKERS:
        if page.locator(sel).count() > 0:
            return f"login:{sel}"
    return None


def locate(page, key: str):
    """Field locator: by name, then id, then label text."""
    for sel in (f"[name='{key}']", f"#{key}"):
        loc = page.locator(sel)
        if loc.count() > 0:
            return loc.first
    loc = page.get_by_label(key, exact=False)
    return loc.first if loc.count() > 0 else None


def fill_field(page, key: str, meta: dict) -> bool:
    """Fill one field from its resolved answer. Returns success."""
    loc = locate(page, key)
    if loc is None:
        return False
    kind, value = meta.get("kind", "text"), meta.get("value", "")
    human_pause()
    try:
        if kind == "file":
            loc.set_input_files(value)
        elif kind == "select":
            loc.select_option(label=value)
        elif kind == "checkbox":
            if str(value).lower() in ("yes", "true", "1", "on"):
                loc.check()
        else:
            loc.fill(str(value))
        return True
    except Exception:
        log.exception("failed to fill %s", key)
        return False


def read_back(page, key: str, meta: dict) -> str:
    loc = locate(page, key)
    if loc is None:
        return "<missing>"
    try:
        if meta.get("kind") == "file":
            return meta.get("value", "")  # file inputs can't be read back; trust set call
        if meta.get("kind") == "checkbox":
            return "Yes" if loc.is_checked() else "No"
        return loc.input_value()
    except Exception:
        return "<unreadable>"


def verify(page, answers: dict) -> dict[str, tuple[str, str]]:
    """Diff intended vs actual for every filled field. Empty dict == clean."""
    diffs = {}
    for key, meta in answers.items():
        if not meta.get("value"):
            continue
        actual = read_back(page, key, meta)
        intended = str(meta["value"])
        if meta.get("kind") != "file" and actual.strip() != intended.strip():
            diffs[key] = (intended, actual)
    return diffs


def find_success(page) -> bool:
    content = page.content().lower()
    return any(p in content for p in SUCCESS_PATTERNS)


def run_application(app_row: dict, answers: dict, *, dry_run: bool,
                    screenshot_path: str, unmapped_handler=None) -> dict:
    """Full fill + verify + (unless dry_run) submit for one application.

    Returns {"outcome": "submitted"|"dry_run_ok"|"needs_human"|"failed",
             "reason": str, "unfilled": [...]}.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            user_data_dir="/app/data/browser_profile", headless=False,
            viewport={"width": 1366, "height": 900})
        page = browser.new_page()
        try:
            page.goto(app_row["apply_url"], wait_until="networkidle", timeout=60000)
            blocker = detect_blocker(page)
            if blocker:
                return {"outcome": "needs_human", "reason": blocker, "unfilled": []}

            unfilled = [k for k, meta in answers.items()
                        if meta.get("value") and not fill_field(page, k, meta)]
            if unfilled and unmapped_handler is not None:
                unfilled = unmapped_handler(page, unfilled, answers)
            if unfilled:
                return {"outcome": "needs_human",
                        "reason": f"unmapped fields: {unfilled}", "unfilled": unfilled}

            diffs = verify(page, answers)
            if diffs:
                return {"outcome": "needs_human", "reason": f"verification diff: {diffs}",
                        "unfilled": []}

            page.screenshot(path=screenshot_path, full_page=True)
            if dry_run:
                return {"outcome": "dry_run_ok", "reason": "", "unfilled": []}

            submit = page.locator("button[type='submit'], input[type='submit']").first
            submit.click()
            page.wait_for_load_state("networkidle", timeout=60000)
            if detect_blocker(page):
                return {"outcome": "needs_human", "reason": "captcha after submit",
                        "unfilled": []}
            page.screenshot(path=screenshot_path.replace(".png", "_after.png"),
                            full_page=True)
            if find_success(page):
                return {"outcome": "submitted", "reason": "", "unfilled": []}
            return {"outcome": "needs_human", "reason": "no confirmation detected",
                    "unfilled": []}
        finally:
            browser.close()
