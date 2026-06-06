"""
Email notifications — OFF by default.
=====================================
When a doctor submits a request, the app can email you. This is disabled until
you set three environment variables, so the app runs fine with no setup.

To turn ON (example for a Yahoo account):
    export HB_SMTP_HOST=smtp.mail.yahoo.com
    export HB_SMTP_PORT=587
    export HB_SMTP_USER=acubilling@yahoo.com
    export HB_SMTP_PASS=your_app_password      # Yahoo requires an "app password"
    export HB_NOTIFY_TO=acubilling@yahoo.com    # where alerts go (can be same)

Notes:
  - Yahoo/Gmail block your normal password for apps; you must generate an
    "app password" in your account security settings and use that here.
  - If these aren't set, send_notification() just prints to the console and
    returns quietly — the in-app queue still works either way.
"""

import os
import smtplib
from email.message import EmailMessage


def email_enabled():
    return all(os.environ.get(k) for k in
               ("HB_SMTP_HOST", "HB_SMTP_USER", "HB_SMTP_PASS", "HB_NOTIFY_TO"))


def send_notification(subject, body):
    """Send an email if configured; otherwise log to console. Never crashes."""
    if not email_enabled():
        print(f"[notify] (email off) {subject} :: {body}")
        return False

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = os.environ["HB_SMTP_USER"]
        msg["To"] = os.environ["HB_NOTIFY_TO"]
        msg.set_content(body)

        host = os.environ["HB_SMTP_HOST"]
        port = int(os.environ.get("HB_SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls()
            s.login(os.environ["HB_SMTP_USER"], os.environ["HB_SMTP_PASS"])
            s.send_message(msg)
        print(f"[notify] email sent: {subject}")
        return True
    except Exception as e:
        # Never let an email failure break the request flow.
        print(f"[notify] email FAILED ({e}); request still saved.")
        return False
