from jobpipe.config import Settings


def test_redacted_hides_secrets():
    s = Settings(anthropic_api_key="sk-ant-secretsecret", smtp_password="hunter2hunter2")
    r = s.redacted()
    assert "secretsecret" not in r["anthropic_api_key"]
    assert "hunter2" not in r["smtp_password"] or r["smtp_password"].endswith("…redacted")
    assert r["smtp_password"].endswith("…redacted")
