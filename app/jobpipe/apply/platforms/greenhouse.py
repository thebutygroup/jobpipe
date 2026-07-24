"""Greenhouse applier — the friendliest platform (server-rendered forms,
stable field names) and the MVP target.

Quirks encoded here (from the Job Board API docs, which document the exact
field schema even though third parties cannot POST to it):
- required basics: first_name, last_name, email; phone usually present
- resume upload input is named `resume`; pdf/doc/docx/txt/rtf accepted
- the application form lives ON the job page (`#application-form` /
  `#application_form`), so extraction hits final_url directly
- custom questions use stable `job_application[answers_attributes]` names —
  the generic extractor reads them fine; we keep its output.
"""

from __future__ import annotations

from .base import PlatformApplier, register


class GreenhouseApplier(PlatformApplier):
    name = "greenhouse"
    needs_browser = False
    success_signals = PlatformApplier.success_signals + (
        "your application was submitted successfully",
    )


register(GreenhouseApplier())
