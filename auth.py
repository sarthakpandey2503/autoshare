import os
import secrets
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from dotenv import load_dotenv
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from database import get_db
import models

load_dotenv()

SECRET_KEY = os.environ.get("AUTOSHARE_SECRET_KEY", "dev-only-change-me-autoshare-secret")
SESSION_COOKIE = "session_token"
SESSION_DAYS = 365
OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN_SECONDS = 60


class NotAuthenticated(Exception):
    """Raised when a protected route is hit without a valid session."""


def hash_otp(otp: str) -> str:
    return bcrypt.hashpw(otp.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_otp(otp: str, otp_hash: str) -> bool:
    try:
        return bcrypt.checkpw(otp.encode("utf-8"), otp_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def create_session(db: Session, student_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    row = models.UserSession(
        id=token,
        student_id=student_id,
        created_at=now,
        expires_at=now + timedelta(days=SESSION_DAYS),
    )
    db.add(row)
    db.commit()
    return token


def set_session_cookie(request: Request, token: str) -> None:
    request.session.clear()
    request.session[SESSION_COOKIE] = token


def clear_session_cookie(request: Request) -> None:
    request.session.clear()


def get_session_token(request: Request) -> Optional[str]:
    token = request.session.get(SESSION_COOKIE)
    return token if isinstance(token, str) and token else None


def logout_current(request: Request, db: Session) -> None:
    token = get_session_token(request)
    if token:
        db.query(models.UserSession).filter(models.UserSession.id == token).delete()
        db.commit()
    clear_session_cookie(request)


def logout_all_devices(request: Request, db: Session, student_id: int) -> None:
    db.query(models.UserSession).filter(models.UserSession.student_id == student_id).delete()
    db.commit()
    clear_session_cookie(request)


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> models.Student:
    token = get_session_token(request)
    if not token:
        raise NotAuthenticated()

    now = datetime.now()
    row = (
        db.query(models.UserSession)
        .filter(
            models.UserSession.id == token,
            models.UserSession.expires_at > now,
        )
        .first()
    )
    if not row:
        clear_session_cookie(request)
        raise NotAuthenticated()

    student = db.query(models.Student).filter(models.Student.id == row.student_id).first()
    if not student:
        db.delete(row)
        db.commit()
        clear_session_cookie(request)
        raise NotAuthenticated()
    return student


def get_optional_user(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[models.Student]:
    token = get_session_token(request)
    if not token:
        return None
    now = datetime.now()
    row = (
        db.query(models.UserSession)
        .filter(
            models.UserSession.id == token,
            models.UserSession.expires_at > now,
        )
        .first()
    )
    if not row:
        return None
    return db.query(models.Student).filter(models.Student.id == row.student_id).first()


def latest_challenge(db: Session, email: str) -> Optional[models.OtpChallenge]:
    return (
        db.query(models.OtpChallenge)
        .filter(models.OtpChallenge.email == email)
        .order_by(models.OtpChallenge.created_at.desc())
        .first()
    )


def can_resend_otp(challenge: models.OtpChallenge) -> bool:
    elapsed = (datetime.now() - challenge.created_at).total_seconds()
    return elapsed >= OTP_RESEND_COOLDOWN_SECONDS


def create_otp_challenge(
    db: Session,
    *,
    email: str,
    purpose: str,
    phone: str | None = None,
    gender: str | None = None,
    username: str | None = None,
) -> tuple[models.OtpChallenge, str]:
    """Create a new OTP challenge; returns (challenge, plaintext_otp)."""
    db.query(models.OtpChallenge).filter(models.OtpChallenge.email == email).delete()
    otp = generate_otp()
    now = datetime.now()
    challenge = models.OtpChallenge(
        email=email,
        otp_hash=hash_otp(otp),
        purpose=purpose,
        phone=phone,
        gender=gender,
        username=username,
        expires_at=now + timedelta(minutes=OTP_TTL_MINUTES),
        attempts=0,
        created_at=now,
    )
    db.add(challenge)
    db.commit()
    db.refresh(challenge)
    return challenge, otp
