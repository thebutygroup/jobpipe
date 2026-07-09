from jobpipe.prepare.answers import resolve
from jobpipe.prepare.forms import FormField, extract_from_html
from jobpipe.profile import (Documents, Eligibility, Identity, Preferences,
                             Profile, Salary)

GH_FORM = """
<form id="application_form">
  <label for="first_name">First Name *</label>
  <input id="first_name" name="first_name" required>
  <label for="email">Email *</label><input id="email" name="email" type="email" required>
  <label for="phone">Phone</label><input id="phone" name="phone" type="tel">
  <label for="sponsor">Do you require visa sponsorship? *</label>
  <select id="sponsor" name="sponsor" required>
    <option></option><option>Yes</option><option>No</option></select>
  <label for="salary">Salary expectation</label><input id="salary" name="salary">
  <label for="why">Why do you want to work here? *</label>
  <textarea id="why" name="why" required></textarea>
  <label for="resume">Resume *</label><input id="resume" name="resume" type="file" required>
  <input type="hidden" name="csrf" value="x">
  <button type="submit">Apply</button>
</form>"""


def _profile(sponsorship=False, salary_pref=95000):
    return Profile(
        identity=Identity(full_name="Joe Buty", email="joe@example.com",
                          location="London, UK",
                          links={"phone": "+44 700 000000", "linkedin": "x"}),
        preferences=Preferences(target_titles=["Senior Data Engineer"],
                                locations_ok=["London"]),
        eligibility=Eligibility(requires_sponsorship=sponsorship,
                                salary=Salary(min=85000, preferred=salary_pref)),
        documents=Documents(resume_default="assets/resume.pdf"),
    )


def test_extract_fields_skips_hidden_and_submit():
    fields = extract_from_html(GH_FORM)
    keys = {f.key for f in fields}
    assert keys == {"first_name", "email", "phone", "sponsor", "salary", "why", "resume"}
    assert next(f for f in fields if f.key == "why").kind == "textarea"
    assert next(f for f in fields if f.key == "sponsor").options == ["Yes", "No"]
    assert next(f for f in fields if f.key == "resume").kind == "file"


def test_identity_resolution():
    p = _profile()
    assert resolve(FormField("first_name", "First Name", "text"), p).value == "Joe"
    assert resolve(FormField("email", "Email", "email"), p).value == "joe@example.com"


def test_sponsorship_resolves_structured_only():
    p = _profile(sponsorship=False)
    f = FormField("sponsor", "Do you require visa sponsorship?", "select",
                  options=["Yes", "No"])
    r = resolve(f, p)
    assert r.value == "No" and r.source == "structured" and not r.llm


def test_sponsorship_unknown_when_unset():
    p = _profile()
    p.eligibility.requires_sponsorship = None
    f = FormField("sponsor", "Do you require visa sponsorship?", "select",
                  options=["Yes", "No"])
    assert resolve(f, p).unknown


def test_salary_never_llm():
    p = _profile(salary_pref=95000)
    r = resolve(FormField("salary", "Salary expectation", "text"), p)
    assert r.source == "structured" and not r.llm and "95" in r.value


def test_free_text_routes_to_llm():
    p = _profile()
    r = resolve(FormField("why", "Why do you want to work here?", "textarea"), p)
    assert r.llm and r.source == "llm"


def test_resume_file_resolves():
    p = _profile()
    r = resolve(FormField("resume", "Resume", "file"), p)
    assert r.source == "file" and r.value == "assets/resume.pdf"


def test_compliance_select_no_fuzzy_guess():
    # A sponsorship value that doesn't cleanly map must be UNKNOWN, not guessed.
    p = _profile(sponsorship=True)
    f = FormField("sponsor", "visa sponsorship", "select",
                  options=["I have the right to work", "I need a different visa"])
    assert resolve(f, p).unknown  # "Yes" matches neither cleanly
