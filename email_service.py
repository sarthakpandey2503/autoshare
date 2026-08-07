"""OTP email delivery via Gmail SMTP (App Password)."""

import os
import smtplib
from email.message import EmailMessage

from dotenv import load_dotenv

load_dotenv()

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "if.not.me.then.who25@gmail.com")
SMTP_APP_PASSWORD = os.environ.get("SMTP_APP_PASSWORD", "")


class EmailSendError(Exception):
    pass


def smtp_configured() -> bool:
    return bool(SMTP_EMAIL and SMTP_APP_PASSWORD)


def send_otp_email(to_email: str, otp_code: str) -> None:
    if not smtp_configured():
        raise EmailSendError(
            "Email is not configured. Set SMTP_EMAIL and SMTP_APP_PASSWORD in .env."
        )

    msg = EmailMessage()
    msg["Subject"] = "AutoShare login code"
    msg["From"] = f"AutoShare (no-reply) <{SMTP_EMAIL}>"
    msg["To"] = to_email
    msg["Reply-To"] = "noreply@iiitkottayam.ac.in"
    msg.set_content(
        f"""Your AutoShare verification code is: {otp_code}

This code expires in 10 minutes.

This is an automated message. Please do not reply to this email.

If you did not request this code, you can ignore this message.
"""
    )

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(SMTP_EMAIL, SMTP_APP_PASSWORD)
            server.send_message(msg)
    except Exception as exc:
        raise EmailSendError("Could not send verification email. Try again later.") from exc
