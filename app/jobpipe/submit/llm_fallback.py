"""Bounded LLM fallback for fields the deterministic filler couldn't locate.

Constraint (non-negotiable): the model chooses WHERE to put values — it may
only place values that already exist in the approved answers. Any action whose
value is not verbatim in the approved set is refused. Budget: 12 actions.
"""

from __future__ import annotations

import json
import logging

from ..config import settings

log = logging.getLogger(__name__)
MAX_ACTIONS = 12

PROMPT = """You are helping fill a job-application form. Some fields could not be
located by selector. You may ONLY move existing approved values into the form;
you may not invent, rephrase, or modify any value.

REMAINING FIELDS (label -> approved value):
{remaining}

PAGE ACCESSIBILITY SNAPSHOT (truncated):
{snapshot}

Respond with ONLY one JSON action, no prose:
{{"action": "fill"|"select"|"click", "selector": "<css selector>",
 "value": "<one of the approved values verbatim, or empty for click>",
 "field_key": "<which remaining field this addresses>"}}
If nothing more can be done, respond: {{"action": "stop"}}"""


def run(page, unfilled: list[str], answers: dict, client=None) -> list[str]:
    """Try to place remaining values. Returns the still-unfilled list."""
    if client is None:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    approved_values = {str(answers[k].get("value", "")) for k in unfilled}
    remaining = set(unfilled)

    for _ in range(MAX_ACTIONS):
        if not remaining:
            break
        snapshot = json.dumps(page.accessibility.snapshot() or {})[:6000]
        listing = "\n".join(f"- {answers[k].get('label', k)!r} -> {answers[k].get('value')!r}"
                            for k in remaining)
        resp = client.messages.create(
            model=settings.match_model, max_tokens=300, temperature=0,
            messages=[{"role": "user",
                       "content": PROMPT.format(remaining=listing, snapshot=snapshot)}])
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        try:
            action = json.loads(text.strip().strip("`"))
        except json.JSONDecodeError:
            log.warning("fallback returned non-JSON; stopping")
            break
        if action.get("action") == "stop":
            break
        value = str(action.get("value", ""))
        if action["action"] in ("fill", "select") and value not in approved_values:
            log.warning("fallback proposed non-approved value %r; refused", value[:50])
            continue
        try:
            loc = page.locator(action["selector"]).first
            if action["action"] == "fill":
                loc.fill(value)
            elif action["action"] == "select":
                loc.select_option(label=value)
            else:
                loc.click()
            key = action.get("field_key")
            if key in remaining:
                remaining.discard(key)
        except Exception:
            log.exception("fallback action failed: %s", action)
    return sorted(remaining)
