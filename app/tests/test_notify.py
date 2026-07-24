from unittest import mock

from jobpipe import notify


def test_unconfigured_smtp_skips_quietly(monkeypatch):
    from jobpipe.config import settings

    monkeypatch.setattr(settings, "smtp_user", "")
    assert notify.send_email("s", "<p>b</p>") is False


def test_send_retries_once_then_fails(monkeypatch):
    from jobpipe.config import settings

    monkeypatch.setattr(settings, "smtp_user", "me@zoho.eu")
    monkeypatch.setattr(settings, "smtp_password", "app-pass")
    monkeypatch.setattr(settings, "notify_to", "me@zoho.eu")
    with mock.patch("smtplib.SMTP_SSL", side_effect=OSError("boom")) as m:
        assert notify.send_email("s", "<p>b</p>") is False
        assert m.call_count == 2  # one retry


def test_send_success(monkeypatch):
    from jobpipe.config import settings

    monkeypatch.setattr(settings, "smtp_user", "me@zoho.eu")
    monkeypatch.setattr(settings, "smtp_password", "app-pass")
    monkeypatch.setattr(settings, "notify_to", "me@zoho.eu")
    with mock.patch("smtplib.SMTP_SSL") as m:
        assert notify.send_email("subject", "<p>body</p>") is True
        server = m.return_value.__enter__.return_value
        server.login.assert_called_once_with("me@zoho.eu", "app-pass")
        args = server.sendmail.call_args[0]
        assert args[0] == "me@zoho.eu" and args[1] == ["me@zoho.eu"]


# ---- transport + branded sender ------------------------------------------------------

class _FakeServer:
    def __init__(self):
        self.sent = []
        self.starttls_called = False
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def login(self, u, p): self.creds = (u, p)
    def starttls(self, context=None): self.starttls_called = True
    def sendmail(self, frm, to, body): self.sent.append((frm, to, body))


def _wire(monkeypatch, port):
    from jobpipe import notify
    from jobpipe.config import settings
    monkeypatch.setattr(settings, "smtp_user", "login@x.com")
    monkeypatch.setattr(settings, "smtp_password", "pw")
    monkeypatch.setattr(settings, "notify_to", "joe@gmail.com")
    monkeypatch.setattr(settings, "smtp_port", port)
    fake = _FakeServer()
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", lambda *a, **k: fake)
    monkeypatch.setattr(notify.smtplib, "SMTP", lambda *a, **k: fake)
    return notify, fake


def test_mail_from_overrides_login(monkeypatch):
    notify, fake = _wire(monkeypatch, 465)
    from jobpipe.config import settings
    monkeypatch.setattr(settings, "mail_from", "jobs@thebutygroup.com")
    assert notify.send_email(subject="s", html_body="<p>x</p>")
    frm, to, body = fake.sent[0]
    assert frm == "jobs@thebutygroup.com" and to == ["joe@gmail.com"]
    assert "From: jobs@thebutygroup.com" in body
    assert not fake.starttls_called  # port 465 = implicit SSL


def test_port_587_uses_starttls(monkeypatch):
    notify, fake = _wire(monkeypatch, 587)
    from jobpipe.config import settings
    monkeypatch.setattr(settings, "mail_from", "")
    assert notify.send_email(subject="s", html_body="x", to="user@y.com")
    assert fake.starttls_called
    assert fake.sent[0][0] == "login@x.com"  # blank MAIL_FROM -> smtp_user
