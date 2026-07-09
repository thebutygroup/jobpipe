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
