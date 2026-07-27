import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from jobpipe import db as dbmod


@pytest.fixture()
def conn(tmp_path):
    c = dbmod.connect(str(tmp_path / "test.db"))
    yield c
    c.close()


@pytest.fixture()
def profile():
    from jobpipe.profile import Identity, Preferences, Profile

    return Profile(
        identity=Identity(full_name="Test User", location="London, UK"),
        preferences=Preferences(
            target_titles=["Forward Deployed Engineer", "Senior Data Engineer",
                           "Machine Learning Engineer", "Founding Engineer",
                           "Data Engineering Manager", "Applied AI Engineer"],
            # the ex-DEFAULT_SYNONYMS now live on the profile (as they do in
            # the owner's real profile.yaml) — no more code-level synonyms
            # that leak onto every applicant
            title_synonyms=["FDE", "Forward Deployed", "AI Engineer",
                            "Member of Technical Staff", "Solutions Engineer AI"],
            locations_ok=["London", "Remote (UK)", "Hybrid", "United Kingdom"],
            hard_nos=["Paris only", "requires relocation"],
        ),
        positioning_summary="Integration over capability.",
    )
