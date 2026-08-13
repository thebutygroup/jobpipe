"""Candidate fit — the OTHER direction of the bidirectional match (R2).

Role fit (the existing matcher prompt) asks: does this job match what the
user wants? THIS module asks: does the user match what the job needs —
based on their resume text and profile evidence.

Deliberately standalone (design R2): pure functions, no DB, no API client.
R3 composes build_block() into the matcher's prompt at assembly time so
both verdicts ride ONE model call; until then nothing imports this except
its tests.

Ground rules (non-negotiable, from the workplan):
- Positive framing: never "lack", "missing", or "gap" — the pattern is
  "this experience isn't on your resume — for this role it should be."
- Grounded evidence only: `bring` must be evidenced in the resume text;
  `unlisted` must be evidenced in the profile fields; nothing is invented.
- Resume-less users never see this block at all (R3 gates on resume text).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ~1,500 tokens of resume ≈ 6,000 chars; cut at a paragraph boundary so the
# model never sees a sentence sliced mid-word.
MAX_RESUME_CHARS = 6_000


class CandidateFit(BaseModel):
    """The three extra output fields the combined call returns (R3)."""

    candidate_fit: int = Field(ge=0, le=10)
    bring: list[str] = Field(default_factory=list)      # grounded in resume
    unlisted: list[str] = Field(default_factory=list)   # grounded in profile


def truncate_resume(text: str, limit: int = MAX_RESUME_CHARS) -> str:
    """Cap resume text at a paragraph boundary within `limit` chars."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text.rfind("\n\n", 0, limit)
    if cut < limit // 2:                 # no useful boundary — cut on a line
        cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:                 # wall of text — hard cut at a space
        cut = text.rfind(" ", 0, limit)
    return text[: cut if cut > 0 else limit].rstrip()


def evidence_fields(raw_yaml_data: dict) -> dict[str, str]:
    """The profile fields `unlisted` may draw on, as plain strings. These are
    the user's OWN words about themselves (experience, skills, positioning) —
    the only legitimate source for 'evidenced but not on the resume'."""
    data = raw_yaml_data or {}

    def _s(v) -> str:
        if isinstance(v, list):
            return ", ".join(str(x) for x in v)
        return str(v or "").strip()

    return {
        "experience": _s(data.get("experience")),
        "skills": _s(data.get("skills")),
        "positioning": _s(data.get("positioning_summary")
                          or data.get("positioning")),
    }


# The block is .format()-ed with resume_text + the three evidence fields.
# R3 appends OUTPUT_SPEC to the role-fit prompt's output instructions.
BLOCK_TEMPLATE = """\

CANDIDATE FIT — the second verdict (in the same JSON reply):
Assess how well THIS CANDIDATE matches what THIS JOB needs, using ONLY the
evidence below. Score candidate_fit 0-10.

Candidate's resume (verbatim extract):
---
{resume_text}
---

Also evidenced, in the candidate's own profile words:
- experience: {experience}
- skills: {skills}
- positioning: {positioning}

Evidence rules — these override everything else:
1. "bring": the candidate's strengths FOR THIS ROLE, each one grounded in
   the resume text above (e.g. industry experience the description asks
   for). If the resume shows nothing relevant, return an empty list.
2. "unlisted": things this job values that do not appear in the resume
   text but ARE evidenced in the profile fields above. Empty list if
   nothing qualifies. NEVER include anything not evidenced somewhere above.
3. Framing: every item is written as a strength or an opportunity —
   "isn't on your resume yet — for this role it should be", never as a
   deficiency. Do not use negative framing words.
4. Do not repeat an item in both lists. Keep items short and specific.
"""

OUTPUT_SPEC = """\
Additionally include in the SAME JSON object:
  "candidate_fit": <0-10 integer>,
  "bring": ["strength grounded in the resume", ...],
  "unlisted": ["job-valued item evidenced only in the profile", ...]
"""


def build_block(resume_text: str, raw_yaml_data: dict | None = None) -> str:
    """The CANDIDATE FIT prompt segment, or "" when there is no resume text
    (resume-less users' prompts must stay byte-identical to today)."""
    resume_text = (resume_text or "").strip()
    if not resume_text:
        return ""
    ev = evidence_fields(raw_yaml_data or {})
    return BLOCK_TEMPLATE.format(
        resume_text=truncate_resume(resume_text),
        experience=ev["experience"] or "(none given)",
        skills=ev["skills"] or "(none given)",
        positioning=ev["positioning"] or "(none given)",
    )


def parse_candidate(payload: dict) -> CandidateFit | None:
    """Pull the candidate-fit fields out of the combined JSON reply.
    Returns None when the fields are absent (legacy cached responses,
    resume-less evaluations) — callers store NULLs, never guesses."""
    if not isinstance(payload, dict) or "candidate_fit" not in payload:
        return None
    return CandidateFit(
        candidate_fit=int(payload.get("candidate_fit", 0)),
        bring=[str(x) for x in (payload.get("bring") or [])][:10],
        unlisted=[str(x) for x in (payload.get("unlisted") or [])][:10],
    )
