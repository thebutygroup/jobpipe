"""R2: the candidate-fit block and parser, standalone — no DB, no API.
The retail fixture is the canonical grounding case from the design."""

import pytest
from pydantic import ValidationError

from jobpipe.matching import candidate_fit as cf

RESUME = ("Senior Data Engineer at AdTech Ltd (2019-2026). Built Spark and "
          "dbt pipelines for campaign analytics. Led a team of five. "
          "Python, SQL, AWS. ")

PROFILE_DATA = {
    "experience": "Two years retail apparel buying at a national chain "
                  "before moving into data.",
    "skills": ["Python", "Spark", "stakeholder management"],
    "positioning_summary": "Integration over capability.",
}


def test_block_contains_resume_evidence_and_grounding_rules():
    block = cf.build_block(RESUME, PROFILE_DATA)
    assert "AdTech Ltd" in block                       # resume text present
    assert "retail apparel" in block                   # profile evidence present
    assert "stakeholder management" in block           # lists flattened
    assert "NEVER include anything not evidenced" in block
    assert "empty list" in block.lower()


def test_positive_framing_words_are_banned_from_the_prompt():
    block = cf.build_block(RESUME, PROFILE_DATA)
    for word in ("lack", "missing", "gap"):
        assert word not in block.lower(), word
    for word in ("lack", "missing", "gap"):
        assert word not in cf.OUTPUT_SPEC.lower(), word


def test_no_resume_means_no_block_at_all():
    assert cf.build_block("", PROFILE_DATA) == ""
    assert cf.build_block(None, PROFILE_DATA) == ""
    # byte-identical prompts for resume-less users depend on this exact ""


def test_truncation_cuts_at_a_paragraph_boundary():
    long = "\n\n".join(f"Paragraph {i} " + "x" * 200 for i in range(60))
    out = cf.truncate_resume(long)
    assert len(out) <= cf.MAX_RESUME_CHARS
    assert out.endswith(tuple("x0123456789"))          # no mid-escape cut
    assert "Paragraph 0" in out
    # a boundary cut, not a hard slice: the tail is a complete paragraph
    assert not out.endswith("\n")


def test_evidence_fields_handle_missing_and_list_values():
    ev = cf.evidence_fields({})
    assert ev == {"experience": "", "skills": "", "positioning": ""}
    ev = cf.evidence_fields(PROFILE_DATA)
    assert ev["skills"] == "Python, Spark, stakeholder management"
    assert ev["positioning"] == "Integration over capability."


def test_parse_candidate_roundtrip_and_absence():
    got = cf.parse_candidate({
        "score": 8,                                    # role-fit fields ignored
        "candidate_fit": 7,
        "bring": ["Spark pipelines at scale"],
        "unlisted": ["Retail apparel experience — this JD values retail"],
    })
    assert got.candidate_fit == 7
    assert got.bring == ["Spark pipelines at scale"]
    assert "Retail apparel" in got.unlisted[0]
    # legacy / resume-less replies: fields absent -> None, never a guess
    assert cf.parse_candidate({"score": 8, "reasons": []}) is None
    assert cf.parse_candidate("not a dict") is None


def test_parse_candidate_rejects_out_of_range_scores():
    with pytest.raises(ValidationError):
        cf.parse_candidate({"candidate_fit": 14})
    with pytest.raises(ValidationError):
        cf.parse_candidate({"candidate_fit": -1})


def test_parse_candidate_caps_list_lengths():
    got = cf.parse_candidate({"candidate_fit": 5, "bring": ["x"] * 40})
    assert len(got.bring) == 10