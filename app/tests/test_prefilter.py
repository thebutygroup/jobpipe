import pytest

from jobpipe.matching.prefilter import classify
from jobpipe.models import PREFILTERED, REJECTED_AUTO

CASES = [
    # (title, location, expected)
    ("Senior Data Engineer", "London, UK", PREFILTERED),
    ("Data Engineering Manager", "Hybrid - London", PREFILTERED),
    ("Forward Deployed Engineer", "London", PREFILTERED),
    ("Forward-Deployed Engineer, AI", "Remote (UK)", PREFILTERED),
    ("FDE - EMEA", "London", PREFILTERED),
    ("Founding Engineer", "", PREFILTERED),                       # unknown location kept
    ("Machine Learning Engineer", "United Kingdom", PREFILTERED),
    ("Applied AI Engineer", "Hybrid, London", PREFILTERED),
    ("Member of Technical Staff", "London", PREFILTERED),          # default synonym
    ("Staff Machine Learning Engineer", "London", PREFILTERED),    # contains target
    ("Senior Data Engineer", "Paris only", REJECTED_AUTO),         # hard no
    ("Senior Data Engineer", "New York, USA", REJECTED_AUTO),      # not in locations_ok
    ("Accountant", "London", REJECTED_AUTO),
    ("Marketing Manager", "London", REJECTED_AUTO),
    ("Data Analyst", "London", REJECTED_AUTO),
    ("Sales Engineer", "London", REJECTED_AUTO),
    ("Engineering Manager, Platform", "London", REJECTED_AUTO),    # not a target title
    ("Junior Data Scientist", "Remote (UK)", REJECTED_AUTO),
    ("Product Manager, AI", "London", REJECTED_AUTO),
    ("HR Business Partner", "Hybrid London", REJECTED_AUTO),
]


@pytest.mark.parametrize("title,location,expected", CASES)
def test_classify(title, location, expected, profile):
    state, _reason = classify(title, location, profile)
    assert state == expected, f"{title!r} @ {location!r}"
