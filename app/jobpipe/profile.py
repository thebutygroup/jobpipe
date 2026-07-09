"""Applicant profile loading + validation (v0 subset).

v0 needs identity, preferences and a positioning summary for the matcher.
The full answers-bank / eligibility schema arrives in v1; keys are accepted
but not required here.
"""

from __future__ import annotations

import yaml
from pydantic import BaseModel, Field, ValidationError


class Identity(BaseModel):
    full_name: str
    email: str = ""
    location: str = ""
    links: dict[str, str] = Field(default_factory=dict)


class Preferences(BaseModel):
    target_titles: list[str] = Field(min_length=1)
    title_synonyms: list[str] = Field(default_factory=list)
    locations_ok: list[str] = Field(min_length=1)
    hard_nos: list[str] = Field(default_factory=list)


class Salary(BaseModel):
    currency: str = "GBP"
    min: int | None = None
    preferred: int | None = None
    disclose: str = "range_only"  # range_only | exact | decline


class Eligibility(BaseModel):
    work_authorisation: str = ""
    requires_sponsorship: bool | None = None
    notice_period: str = ""
    salary: Salary = Field(default_factory=Salary)


class Documents(BaseModel):
    resume_default: str = ""
    resume_variants: dict[str, str] = Field(default_factory=dict)
    cover_letter_corpus: list[str] = Field(default_factory=list)  # style-reference paths


class BankEntry(BaseModel):
    match: list[str]                       # keywords to match against question text
    answer: str | None = None              # literal answer
    answer_from: str | None = None         # dotted path into profile (structured)
    strategy: str | None = None            # 'llm_generate' for free text
    template_ref: str | None = None


class Profile(BaseModel):
    identity: Identity
    preferences: Preferences
    positioning_summary: str = ""
    eligibility: Eligibility = Field(default_factory=Eligibility)
    documents: Documents = Field(default_factory=Documents)
    answers_bank: list[BankEntry] = Field(default_factory=list)

    def lookup_path(self, dotted: str):
        """Resolve 'eligibility.requires_sponsorship' etc. against the profile."""
        obj = self
        for part in dotted.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                return None
        return obj


class ProfileError(Exception):
    pass


def load_profile_yaml(text: str) -> Profile:
    """Validate a profile from a YAML string (DB-stored profiles)."""
    try:
        return Profile.model_validate(yaml.safe_load(text) or {})
    except ValidationError as e:
        raise ProfileError(f"profile failed validation:\n{e}") from e


def load_applicant_profile(applicant_row) -> Profile:
    """The one true per-applicant loader. DB-stored profile_yaml wins;
    file-based profile_path is the legacy/bootstrap fallback (Joe's
    original profile.yaml). Every stage (match, prepare, submit) must go
    through this so no applicant is ever evaluated - or worse, has
    compliance answers resolved - against someone else's profile."""
    if applicant_row["profile_yaml"]:
        return load_profile_yaml(applicant_row["profile_yaml"])
    return load_profile(applicant_row["profile_path"])


def load_profile(path: str) -> Profile:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return Profile.model_validate(data)
    except FileNotFoundError as e:
        raise ProfileError(f"profile file not found: {path}") from e
    except ValidationError as e:
        raise ProfileError(f"profile failed validation:\n{e}") from e


if __name__ == "__main__":
    from .config import settings

    p = load_profile(settings.profile_path)
    print(f"profile OK: {p.identity.full_name}, {len(p.preferences.target_titles)} target titles")
