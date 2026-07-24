"""Ashby and Workable: JS-rendered application forms.

Static extraction yields zero fields on these platforms (verified in
research) — the browser (Playwright) must extract. Full appliers arrive in
Phase 4; registering the needs_browser knowledge now means routes to these
platforms are handled honestly rather than failing extraction and looking
broken.
"""

from __future__ import annotations

from .base import PlatformApplier, register


class AshbyApplier(PlatformApplier):
    name = "ashby"
    needs_browser = True


class WorkableApplier(PlatformApplier):
    name = "workable"
    needs_browser = True


register(AshbyApplier())
register(WorkableApplier())
