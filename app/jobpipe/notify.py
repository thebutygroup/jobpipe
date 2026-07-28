"""Email notifications via Zoho SMTP (SSL).

Zoho notes:
- Use an APP-SPECIFIC password when 2FA is enabled (Account -> Security -> App Passwords).
- The From address MUST be the authenticated address; Zoho rejects spoofed senders.
- smtp.zoho.eu for UK/EU data-center accounts, smtp.zoho.com for US.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from .config import settings

log = logging.getLogger(__name__)


def send_email(subject: str, html_body: str, text_body: str = "", to: str = "") -> bool:
    """Send one email. Retries once on failure; returns success. Never raises —
    a failed notification must not crash a scheduled job or a signup.

    `to` defaults to NOTIFY_TO (owner alerts); pass an explicit address for
    user-facing mail (e.g. signup confirmations). The From address is always
    the authenticated SMTP user — Zoho rejects anything else."""
    recipient = to or settings.notify_to
    sender = settings.mail_from or settings.smtp_user
    if not (settings.smtp_user and settings.smtp_password and recipient):
        log.warning("SMTP not configured or no recipient; skipping email %r", subject)
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    # Display name + address: inboxes headline the NAME ("jobpipe"), not the
    # bare handle ("jobs"). The address itself is unchanged — DKIM/SPF and
    # the verified sender stay exactly as configured.
    msg["From"] = formataddr((settings.mail_from_name, sender))
    msg["To"] = recipient
    msg.attach(MIMEText(text_body or "See HTML version.", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    for attempt in (1, 2):
        try:
            ctx = ssl.create_default_context()
            with _connect(ctx) as server:
                server.login(settings.smtp_user, settings.smtp_password)
                server.sendmail(sender, [recipient], msg.as_string())
            return True
        except Exception:
            log.exception("email send failed (attempt %d/2): %r", attempt, subject)
    return False


def _connect(ctx):
    """Port 465 = implicit SSL; anything else (587/2525) = STARTTLS. Covers
    Gmail, Zoho and transactional relays (Brevo, SMTP2GO) alike."""
    if int(settings.smtp_port) == 465:
        return smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                                context=ctx, timeout=30)
    server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
    server.starttls(context=ctx)
    return server


def send_failure(job: str, detail: str) -> bool:
    return send_email(
        subject=f"[jobpipe] job failed: {job}",
        html_body=f"<p>Scheduled job <b>{job}</b> failed.</p><pre>{detail}</pre>",
        text_body=f"Scheduled job {job} failed.\n\n{detail}",
    )
