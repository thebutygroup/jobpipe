"""Central settings, loaded from environment / .env via pydantic-settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    notify_to: str = ""
    # IMAP (outcome tracking) may use a different account than SMTP (e.g. a
    # transactional sender like Brevo has no mailbox). Blank = reuse SMTP creds.
    imap_user: str = ""
    imap_password: str = ""

    db_path: str = "/app/data/jobpipe.db"
    profile_path: str = "profile.yaml"
    companies_path: str = "companies.yaml"
    searches_path: str = "searches.yaml"

    # ---- v1: submission (master switch defaults OFF) ----
    submit_enabled: bool = False
    max_submissions_per_day: int = 10
    submit_min_interval_s: int = 300
    company_cooldown_days: int = 30
    captcha_resolver: str = "human"          # human | service
    twocaptcha_api_key: str = ""
    dashboard_base_url: str = "http://localhost:8010"
    imap_host: str = "imap.gmail.com"        # confirmation tracking mailbox
    imap_port: int = 993
    freetext_model: str = "claude-sonnet-4-6"  # prose quality matters more than triage

    indexscan_resolve_cap: int = 100      # careers-resolution attempts per weekly run
    dream_path: str = "dream.yaml"        # manual dream-company list
    crawl_enabled: bool = True
    crawl_page_cap: int = 30              # job pages fetched per domain per run
    crawl_llm_cap: int = 50               # Haiku extraction fallback, per whole run
    constituents_static_path: str = "constituents_static.yaml"

    # ---- aggregator API sources (empty = source stays unconfigured, no-op) ----
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    reed_api_key: str = ""
    aggregator_results_per_page: int = 50
    aggregator_max_pages: int = 2          # pages per search per run
    adzuna_daily_call_cap: int = 100       # free tier ~250/day; stay well under

    # ---- self-serve signup ----
    signup_daily_cap: int = 3        # auto-activated signups per day; beyond -> pending + flag
    signup_instant_matches: int = 20  # postings scored immediately for a new signup

    match_threshold: int = 7
    match_daily_call_cap: int = 200
    match_test_limit: int = 0        # >0 caps matcher to N postings (testing)
    poll_test_limit: int = 0         # >0 caps each poller to N postings (testing)
    builtin_max_pages: int = 30      # result pages walked per Built In saved search
    poll_cooldown_days: float = 0    # >0 skips a board/search polled within N days
    match_model: str = "claude-haiku-4-5"

    request_min_interval_s: float = 1.0
    user_agent: str = "jobpipe/0.1 (personal job-search tool)"

    dashboard_port: int = 8010
    tz: str = "Europe/London"

    def redacted(self) -> dict:
        d = self.model_dump()
        for key in ("anthropic_api_key", "smtp_password", "adzuna_app_key", "reed_api_key"):
            if d.get(key):
                d[key] = d[key][:4] + "…redacted"
        return d


settings = Settings()

if __name__ == "__main__":
    import json

    print(json.dumps(settings.redacted(), indent=2))
