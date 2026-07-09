from jobpipe.submit import captcha, llm_fallback


def test_resolver_selection(monkeypatch):
    from jobpipe.config import settings

    monkeypatch.setattr(settings, "captcha_resolver", "human")
    assert captcha.resolver_for("captcha:iframe[src*='recaptcha']") is \
        captcha.human_pause_resolver

    monkeypatch.setattr(settings, "captcha_resolver", "service")
    # token-solvable -> service
    assert captcha.resolver_for("captcha:.g-recaptcha") is captcha.solve_with_service
    # behavioural -> always human
    assert captcha.resolver_for("captcha:.cf-turnstile") is captcha.human_pause_resolver


class FakeLoc:
    def __init__(self):
        self.filled = None

    def fill(self, v):
        self.filled = v


class FakePage:
    def __init__(self):
        self.accessibility = self
        self._loc = FakeLoc()

    def snapshot(self):
        return {"role": "form"}

    def locator(self, sel):
        class L:
            first = self._loc
        L.first = self._loc
        return L


class FakeClient:
    def __init__(self, actions):
        self.actions = list(actions)
        self.messages = self

    def create(self, **kw):
        import json
        from types import SimpleNamespace

        act = self.actions.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(act))])


def test_fallback_refuses_non_approved_value():
    page = FakePage()
    answers = {"why": {"label": "Why", "value": "approved text"}}
    # model tries to inject a value that isn't in the approved set, then stops
    client = FakeClient([
        {"action": "fill", "selector": "#why", "value": "HALLUCINATED", "field_key": "why"},
        {"action": "stop"},
    ])
    remaining = llm_fallback.run(page, ["why"], answers, client=client)
    assert remaining == ["why"]          # refused; still unfilled
    assert page._loc.filled is None       # nothing was typed


def test_fallback_places_approved_value():
    page = FakePage()
    answers = {"why": {"label": "Why", "value": "approved text"}}
    client = FakeClient([
        {"action": "fill", "selector": "#why", "value": "approved text", "field_key": "why"},
    ])
    remaining = llm_fallback.run(page, ["why"], answers, client=client)
    assert remaining == [] and page._loc.filled == "approved text"
