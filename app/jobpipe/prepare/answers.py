"""Answer resolution: map every form field to an answer, a strategy, or UNKNOWN.

Design rules (non-negotiable):
- COMPLIANCE fields (visa/sponsorship/salary/notice/right-to-work) resolve ONLY
  from structured profile values. If the structured value is missing, the result
  is UNKNOWN — never a bank string, never an LLM guess.
- Select options match exact > contains; a compliance select that doesn't match
  cleanly is UNKNOWN, never fuzzy-guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..profile import Profile
from .forms import FormField

COMPLIANCE_PATTERNS = re.compile(
    r"sponsor|visa|right to work|work authori[sz]ation|immigration|salary|"
    r"compensation|notice period", re.IGNORECASE)

IDENTITY_MAP = [
    (re.compile(r"first name", re.I), lambda p: p.identity.full_name.split()[0]),
    (re.compile(r"last name|surname|family name", re.I),
     lambda p: p.identity.full_name.split()[-1]),
    (re.compile(r"full name|^name$", re.I), lambda p: p.identity.full_name),
    (re.compile(r"e-?mail", re.I), lambda p: p.identity.email),
    (re.compile(r"phone|mobile", re.I), lambda p: p.identity.links.get("phone", "")
     or getattr(p.identity, "phone", "")),
    (re.compile(r"linkedin", re.I), lambda p: p.identity.links.get("linkedin", "")),
    (re.compile(r"github", re.I), lambda p: p.identity.links.get("github", "")),
    (re.compile(r"portfolio|website", re.I), lambda p: p.identity.links.get("portfolio", "")),
    (re.compile(r"location|city|where are you based", re.I), lambda p: p.identity.location),
]


@dataclass
class ResolvedAnswer:
    value: str = ""
    source: str = "unknown"   # structured | bank | identity | llm | file | unknown
    llm: bool = False

    @property
    def unknown(self) -> bool:
        return self.source == "unknown"


def resolve(f: FormField, profile: Profile) -> ResolvedAnswer:
    label = f.label or f.key

    if f.kind == "file":
        resume = profile.documents.resume_default
        return ResolvedAnswer(resume, "file") if resume else ResolvedAnswer()

    if COMPLIANCE_PATTERNS.search(label):
        return _resolve_compliance(f, profile)

    for pattern, getter in IDENTITY_MAP:
        if pattern.search(label):
            value = getter(profile) or ""
            if value:
                return _fit_options(f, value, "identity")
            return ResolvedAnswer()

    bank = _resolve_bank(f, profile)
    if bank is not None:
        return bank

    if f.kind == "textarea":
        return ResolvedAnswer("", "llm", llm=True)  # free text: draft with LLM, review required
    return ResolvedAnswer()


def _resolve_compliance(f: FormField, profile: Profile) -> ResolvedAnswer:
    label = (f.label or "").lower()
    e = profile.eligibility
    if re.search(r"sponsor|visa|immigration", label):
        if e.requires_sponsorship is None:
            return ResolvedAnswer()
        return _fit_options(f, "Yes" if e.requires_sponsorship else "No", "structured",
                            strict=True)
    if re.search(r"right to work|work authori[sz]ation", label):
        return (_fit_options(f, e.work_authorisation, "structured", strict=True)
                if e.work_authorisation else ResolvedAnswer())
    if re.search(r"notice period", label):
        return (_fit_options(f, e.notice_period, "structured", strict=True)
                if e.notice_period else ResolvedAnswer())
    if re.search(r"salary|compensation", label):
        s = e.salary
        if s.disclose == "decline":
            return _fit_options(f, "Prefer not to disclose", "structured", strict=True)
        if s.preferred:
            value = (f"{s.currency} {s.min:,}–{s.preferred:,}"
                     if s.disclose == "range_only" and s.min else f"{s.currency} {s.preferred:,}")
            return _fit_options(f, value, "structured", strict=True)
        return ResolvedAnswer()
    return ResolvedAnswer()


def _resolve_bank(f: FormField, profile: Profile) -> ResolvedAnswer | None:
    label = (f.label or f.key).lower()
    for entry in profile.answers_bank:
        if any(kw.lower() in label for kw in entry.match):
            if entry.answer_from:
                value = profile.lookup_path(entry.answer_from)
                if value is None:
                    return ResolvedAnswer()
                if isinstance(value, bool):
                    value = "Yes" if value else "No"
                return _fit_options(f, str(value), "structured", strict=True)
            if entry.strategy == "llm_generate":
                return ResolvedAnswer("", "llm", llm=True)
            if entry.answer is not None:
                return _fit_options(f, entry.answer, "bank")
    return None


def _fit_options(f: FormField, value: str, source: str, strict: bool = False) -> ResolvedAnswer:
    """Map a value onto a select's options: exact > contains > UNKNOWN.
    strict=True (compliance): no fuzzy fallback beyond contains-one-way."""
    if f.kind != "select" or not f.options:
        return ResolvedAnswer(value, source)
    lowered = value.lower().strip()
    for opt in f.options:
        if opt.lower().strip() == lowered:
            return ResolvedAnswer(opt, source)
    contains = [opt for opt in f.options
                if lowered in opt.lower() or opt.lower() in lowered]
    if len(contains) == 1:
        return ResolvedAnswer(contains[0], source)
    return ResolvedAnswer()  # ambiguous or missing: UNKNOWN, human decides
