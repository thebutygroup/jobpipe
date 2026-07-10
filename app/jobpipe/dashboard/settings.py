"""Minimal Django settings: templating + routing only.

No ORM, no sessions, no in-app auth — the pipeline DB is accessed via
jobpipe.db and authentication happens at the Cloudflare Access edge.
v1 write routes are CSRF-protected; cookie CSRF (no sessions needed).
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "set-me-in-env-for-v1")  # CSRF signing
DEBUG = False
_extra_host = os.environ.get("DASHBOARD_HOST", "")
ALLOWED_HOSTS = ["jobpipe-web", "localhost", "127.0.0.1"] + ([_extra_host] if _extra_host else [])
if os.environ.get("JOBPIPE_TESTING") == "1":
    ALLOWED_HOSTS.append("testserver")

INSTALLED_APPS: list[str] = []
MIDDLEWARE = ["django.middleware.common.CommonMiddleware",
              "django.middleware.csrf.CsrfViewMiddleware"]
ROOT_URLCONF = "jobpipe.dashboard.urls"
DATABASES: dict = {}
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": False,
    "OPTIONS": {"context_processors": []},
}]
USE_TZ = True
# Django's implicit default is America/Chicago and it exports TZ to the whole
# process — pin to the deployment timezone instead (matters for daily caps).
TIME_ZONE = os.environ.get("TZ", "Europe/London")
# TLS terminates at the Cloudflare tunnel; trust its forwarded proto.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

CSRF_TRUSTED_ORIGINS = ([f"https://{_extra_host}"] if _extra_host else [])
