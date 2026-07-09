from jobpipe.models import canonicalise_url
from jobpipe.pollers import ashby, builtin, greenhouse, lever, workable

GH_JOB = {
    "id": 4567,
    "title": "Senior Data Engineer",
    "location": {"name": "London, UK"},
    "departments": [{"name": "Engineering"}],
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/4567",
    "content": "<p>Build &amp; scale pipelines</p>",
}

LEVER_JOB = {
    "id": "ab-12",
    "text": "Forward Deployed Engineer",
    "categories": {"location": "London", "team": "Field Eng", "commitment": "Full-time"},
    "hostedUrl": "https://jobs.lever.co/acme/ab-12",
    "descriptionPlain": "Work with customers",
    "description": "<div>Work with customers</div>",
}


def test_greenhouse_normalise():
    dto = greenhouse.normalise("Acme", GH_JOB)
    assert dto.title == "Senior Data Engineer"
    assert dto.location == "London, UK"
    assert "pipelines" in dto.description_text and "<p>" not in dto.description_text
    assert dto.external_id == "4567"


def test_lever_normalise():
    dto = lever.normalise("Acme", LEVER_JOB)
    assert dto.title == "Forward Deployed Engineer"
    assert dto.department == "Field Eng"
    assert dto.apply_url.endswith("/ab-12")


def test_ashby_normalise():
    dto = ashby.normalise("Acme", {"id": "x1", "title": "Founding Engineer",
                                   "location": "London", "isRemote": False,
                                   "jobUrl": "https://jobs.ashbyhq.com/acme/x1",
                                   "descriptionHtml": "<b>Own everything</b>"})
    assert dto.title == "Founding Engineer" and dto.description_text == "Own everything"


def test_workable_normalise():
    dto = workable.normalise("Acme", {"shortcode": "AB12", "title": "ML Engineer",
                                      "city": "London", "country": "United Kingdom",
                                      "telecommuting": True,
                                      "url": "https://apply.workable.com/acme/j/AB12/",
                                      "description": "<p>Models in prod</p>"})
    assert dto.location == "London, United Kingdom" and dto.remote_policy == "remote"


def test_canonicalise_strips_tracking():
    a = canonicalise_url("https://boards.greenhouse.io/Acme/jobs/1?gh_src=x&utm_source=y")
    b = canonicalise_url("https://boards.greenhouse.io/acme/jobs/1/")
    assert a == b


BUILTIN_HTML = """
<html><body>
<div data-id="job-card">
  <a href="/company/acme-ai">Acme AI</a>
  <a href="/job/senior-data-engineer-acme">Senior Data Engineer</a>
  <span>Hybrid</span> <span>London</span> <span>3 days ago</span>
</div>
<div data-id="job-card">
  <a href="/company/beta">Beta Ltd</a>
  <a href="/job/fde-beta">Forward Deployed Engineer</a>
  <span>In Office</span> <span>London</span>
</div>
<a rel="next" href="/jobs/hybrid/office?page=2&search=x">Next</a>
</body></html>
"""


def test_builtin_parse_cards():
    postings, next_url = builtin.parse_search_page(BUILTIN_HTML)
    assert len(postings) == 2
    assert postings[0].company_name == "Acme AI"
    assert postings[0].title == "Senior Data Engineer"
    assert postings[0].source == "builtin"
    assert next_url and "page=2" in next_url


def test_builtin_fallback_when_no_data_id():
    html = BUILTIN_HTML.replace('data-id="job-card"', "")
    postings, _ = builtin.parse_search_page(html)
    assert len(postings) == 2


def test_detect_ats():
    assert builtin.detect_ats("https://boards.greenhouse.io/acme/jobs/1") == {
        "ats": "greenhouse", "board_token": "acme"}
    assert builtin.detect_ats("https://jobs.ashbyhq.com/beta/uuid") == {
        "ats": "ashby", "board_token": "beta"}
    assert builtin.detect_ats("https://example.com/careers") == {}
