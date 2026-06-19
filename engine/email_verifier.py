"""Fetch LinkedIn's email verification code from Gmail via IMAP.

Used to fully automate LinkedIn login: when LinkedIn challenges with an email
PIN, we read the freshly-sent code from the configured Gmail inbox (app-password
IMAP) and feed it back to the login form — no manual input required.
"""

from __future__ import annotations

import email
import imaplib
import re
import time
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Optional

from config import (
    GMAIL_USERNAME, GMAIL_APP_PASSWORD, GMAIL_IMAP_HOST, GMAIL_IMAP_PORT,
    GMAIL_IMAP_FOLDER, GMAIL_VERIFICATION_SENDER, GMAIL_POLL_INTERVAL,
    GMAIL_POLL_TIMEOUT,
)

_CODE_RE = re.compile(r"\b(\d{6})\b")


def gmail_configured() -> bool:
    return bool(GMAIL_USERNAME) and bool(GMAIL_APP_PASSWORD)


def _decode(s: Optional[str]) -> str:
    if not s:
        return ""
    out = ""
    for part, enc in decode_header(s):
        if isinstance(part, bytes):
            out += part.decode(enc or "utf-8", "ignore")
        else:
            out += part
    return out


def _body_text(msg) -> str:
    txt = ""
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() in ("text/plain", "text/html"):
                try:
                    txt += p.get_payload(decode=True).decode("utf-8", "ignore")
                except Exception:
                    continue
    else:
        try:
            txt = msg.get_payload(decode=True).decode("utf-8", "ignore")
        except Exception:
            txt = ""
    return txt


def _extract_code(subject: str, body: str) -> Optional[str]:
    # The code lives in the subject ("Here's your verification code 620305");
    # prefer subject, fall back to body.
    m = _CODE_RE.search(subject)
    if m:
        return m.group(1)
    m = _CODE_RE.search(body)
    return m.group(1) if m else None


def _msg_epoch(msg) -> float:
    """Epoch of the email's Date header (reliable across servers); 0.0 if unknown."""
    try:
        dt = parsedate_to_datetime(msg.get("Date"))
        return dt.timestamp() if dt else 0.0
    except Exception:
        return 0.0


def _try_fetch(after_epoch: float, already_used: set[str]) -> Optional[str]:
    """One IMAP pass: return the newest LinkedIn verification code from an email
    sent around/after this login attempt. Iterates newest-first and returns the
    first email that carries a fresh, unused 6-digit code."""
    try:
        M = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, GMAIL_IMAP_PORT)
        M.login(GMAIL_USERNAME, (GMAIL_APP_PASSWORD or "").replace(" ", ""))
    except Exception:
        return None

    try:
        M.select(GMAIL_IMAP_FOLDER, readonly=True)
        since_str = datetime.fromtimestamp(after_epoch - 86400, tz=timezone.utc).strftime("%d-%b-%Y")
        sender = GMAIL_VERIFICATION_SENDER or "security-noreply@linkedin.com"
        typ, data = M.search(None, "FROM", sender, "SINCE", since_str)
        if typ != "OK" or not data or not data[0]:
            # Fall back to any LinkedIn sender.
            typ, data = M.search(None, "FROM", "linkedin.com", "SINCE", since_str)
        ids = data[0].split() if data and data[0] else []

        # Accept a code sent shortly before our submit (clock skew tolerance).
        window_start = after_epoch - 180
        for mid in reversed(ids[-25:]):  # newest sequence numbers first
            typ, md = M.fetch(mid, "(RFC822)")
            if typ != "OK" or not md or not md[0]:
                continue
            msg = email.message_from_bytes(md[0][1])
            ep = _msg_epoch(msg)
            if ep and ep < window_start:
                break  # newest-first: everything older lies beyond here
            code = _extract_code(_decode(msg.get("Subject")), _body_text(msg))
            if code and code not in already_used:
                return code
        return None
    except Exception:
        return None
    finally:
        try:
            M.logout()
        except Exception:
            pass


def fetch_verification_code(
    after_epoch: float,
    timeout: Optional[float] = None,
    poll_interval: Optional[float] = None,
    already_used: Optional[set[str]] = None,
) -> Optional[str]:
    """Poll Gmail for a LinkedIn verification code sent after ``after_epoch``.

    Returns the 6-digit code, or None if none arrives within ``timeout`` seconds.
    """
    if not gmail_configured():
        return None
    timeout = timeout if timeout is not None else GMAIL_POLL_TIMEOUT
    poll_interval = poll_interval if poll_interval is not None else GMAIL_POLL_INTERVAL
    already_used = already_used or set()

    deadline = time.time() + timeout
    while time.time() < deadline:
        code = _try_fetch(after_epoch, already_used)
        if code:
            return code
        time.sleep(poll_interval)
    return None
