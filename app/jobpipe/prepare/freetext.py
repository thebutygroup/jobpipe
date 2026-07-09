"""LLM drafting for free-text questions and cover letters.

The LLM writes prose ONLY: it never sees or answers compliance fields (the
resolver routes those away before this module is reached). Drafts are marked
llm=True so the review UI highlights them; nothing ships without human eyes.
"""

from __future__ import annotations

from ..config import settings

QUESTION_PROMPT = """You are drafting an answer for a job application, written in the first
person as the candidate. Max {max_words} words. Plain, specific, no hype, no bullet
points, no em-dash-heavy AI cadence. Ground every claim in the profile below; invent
nothing.

CANDIDATE PROFILE
{profile_summary}

JOB
{company} — {title}
{description_excerpt}

QUESTION
{question}

Respond with the answer text only."""

COVER_PROMPT = """Draft a short cover letter (max 220 words) in the first person as the
candidate below, for the role below. Specific and plain; open with why this role,
close with one concrete relevant achievement. No headers, no address block, no
"Dear Hiring Manager" boilerplate — start with substance. Invent nothing.

CANDIDATE PROFILE
{profile_summary}

ROLE
{company} — {title}
{description_excerpt}

Respond with the letter text only."""


def _call(client, prompt: str) -> str:
    resp = client.messages.create(
        model=settings.freetext_model, max_tokens=500, temperature=0.4,
        messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


def draft_answer(client, profile_summary: str, company: str, title: str,
                 description: str, question: str, max_words: int = 180) -> str:
    return _call(client, QUESTION_PROMPT.format(
        max_words=max_words, profile_summary=profile_summary, company=company,
        title=title, description_excerpt=(description or "")[:3000], question=question))


def draft_cover_letter(client, profile_summary: str, company: str, title: str,
                       description: str) -> str:
    return _call(client, COVER_PROMPT.format(
        profile_summary=profile_summary, company=company, title=title,
        description_excerpt=(description or "")[:3000]))
