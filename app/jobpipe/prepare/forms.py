"""Form field extraction.

Static-first: most Greenhouse/Lever application pages are server-rendered, so
requests + bs4 covers them without a browser. JS-heavy forms (Ashby) yield zero
fields statically; those postings are flagged needs_browser and the submitter
container re-extracts with Playwright.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from bs4 import BeautifulSoup

from ..pollers.base import polite_get

SKIP_TYPES = {"hidden", "submit", "button", "image", "reset"}


@dataclass
class FormField:
    key: str            # stable key: name or id
    label: str
    kind: str           # text | textarea | select | checkbox | radio | file | email | tel | url
    required: bool = False
    options: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def extract_from_url(apply_url: str) -> list[FormField]:
    html = polite_get(apply_url).text
    return extract_from_html(html)


def extract_from_html(html: str) -> list[FormField]:
    soup = BeautifulSoup(html, "html.parser")
    form = _pick_form(soup)
    if form is None:
        return []
    fields: list[FormField] = []
    seen: set[str] = set()
    for el in form.find_all(["input", "textarea", "select"]):
        f = _parse_element(el, soup)
        if f and f.key not in seen:
            seen.add(f.key)
            fields.append(f)
    return fields


def _pick_form(soup: BeautifulSoup):
    for selector in ("#application_form", "form#application-form",
                     "form[action*='greenhouse']", "form[action*='lever']", "form"):
        form = soup.select_one(selector)
        if form:
            return form
    return None


def _parse_element(el, soup) -> FormField | None:
    kind = el.name
    if kind == "input":
        input_type = (el.get("type") or "text").lower()
        if input_type in SKIP_TYPES:
            return None
        kind = input_type if input_type in ("file", "checkbox", "radio", "email",
                                            "tel", "url") else "text"
    key = el.get("name") or el.get("id") or ""
    if not key:
        return None
    label = _label_for(el, soup)
    required = el.has_attr("required") or "required" in (el.get("aria-required", ""),) \
        or "required" in (el.get("class") or [])
    options = []
    if el.name == "select":
        kind = "select"
        options = [o.get_text(strip=True) for o in el.find_all("option")
                   if o.get_text(strip=True) and not o.get("disabled")]
    return FormField(key=key, label=label or key, kind=kind, required=bool(required),
                     options=options)


def _label_for(el, soup) -> str:
    el_id = el.get("id")
    if el_id:
        lab = soup.find("label", attrs={"for": el_id})
        if lab:
            return lab.get_text(" ", strip=True)
    parent_label = el.find_parent("label")
    if parent_label:
        return parent_label.get_text(" ", strip=True)
    return el.get("aria-label") or el.get("placeholder") or ""
