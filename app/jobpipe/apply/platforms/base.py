"""PlatformApplier contract + registry.

An applier knows how ONE platform's application forms behave: how to extract
fields, what success looks like, and any quirks the generic heuristics get
wrong. GenericApplier (the fallback) is today's heuristic behaviour, so
unknown company sites keep working exactly as before.
"""

from __future__ import annotations

from ...prepare.forms import FormField, extract_from_url


class PlatformApplier:
    name = "generic"
    #: some boards prohibit automated submission — such platforms are
    #: registered with permitted=False and always route to manual assist.
    permitted = True
    #: phrases that indicate a successful submission on this platform
    success_signals = (
        "thank you for applying", "application received", "application submitted",
        "we have received your application", "thanks for applying",
        "your application has been submitted",
    )

    def extract(self, final_url: str) -> list[FormField]:
        """Extract the application form's fields. Static-first; platforms
        whose forms are JS-rendered override needs_browser instead."""
        return extract_from_url(final_url)

    #: True => static extraction is known-useless; go straight to Playwright
    needs_browser = False


_REGISTRY: dict[str, PlatformApplier] = {}


def register(applier: PlatformApplier) -> PlatformApplier:
    _REGISTRY[applier.name] = applier
    return applier


def get_applier(platform: str) -> PlatformApplier:
    """Specific applier for the platform, or the generic fallback."""
    return _REGISTRY.get(platform) or _REGISTRY["generic"]


register(PlatformApplier())  # the generic fallback registers itself
