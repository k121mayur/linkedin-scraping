"""Outbound mail — sends new users their credentials via the Gmail account."""

from __future__ import annotations

import smtplib
from email.mime.text import MIMEText

from config import GMAIL_USERNAME, GMAIL_APP_PASSWORD, SMTP_HOST, SMTP_PORT, APP_BASE_URL


def mail_configured() -> bool:
    return bool(GMAIL_USERNAME and GMAIL_APP_PASSWORD)


def send_welcome_email(name: str, email: str, password: str) -> bool:
    """Email the new user their login credentials. Returns True on success.

    Best-effort by design: a mail failure must never block user creation, so
    callers treat False as "user created, email not delivered".
    """
    if not mail_configured():
        return False

    body = (
        f"Hi {name},\n\n"
        f"You have been added as a user on the Tamuku job & grants scraping portal.\n\n"
        f"Your login credentials:\n"
        f"  Email:    {email}\n"
        f"  Password: {password}\n\n"
        f"You can sign in here: {APP_BASE_URL}\n\n"
        f"Regards,\nTamuku"
    )
    msg = MIMEText(body)
    msg["Subject"] = "Your Tamuku portal account"
    msg["From"] = GMAIL_USERNAME
    msg["To"] = email

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.login(GMAIL_USERNAME, GMAIL_APP_PASSWORD)
            smtp.sendmail(GMAIL_USERNAME, [email], msg.as_string())
        return True
    except Exception as e:
        import sys
        print(f"[mailer] Failed to send welcome email to {email}: {e}",
              file=sys.stderr, flush=True)
        return False
