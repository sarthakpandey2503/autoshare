"""Shared ride / account validation helpers."""

import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

import models

MAX_LOCATION_LEN = 100
MAX_FARE = 10_000
MIN_FARE = 1
PHONE_PATTERN = re.compile(r"^(?:\+91[\-\s]?|0)?([6-9]\d{9})$")
EMAIL_PATTERN = re.compile(r"^[a-z0-9._%+-]+@iiitkottayam\.ac\.in$")
ALLOWED_GENDERS = {"male", "female"}
CONFLICT_WINDOW = timedelta(minutes=45)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def normalize_phone(phone: str) -> str:
    """Return a canonical 10-digit mobile number, or empty string if invalid."""
    match = PHONE_PATTERN.fullmatch(phone.strip().replace(" ", ""))
    return match.group(1) if match else ""


def normalize_gender(gender: str) -> str:
    return gender.strip().lower()


def validate_email(email: str) -> str | None:
    if not EMAIL_PATTERN.fullmatch(email):
        return "Use your college email ending with @iiitkottayam.ac.in."
    return None


def validate_gender(gender: str) -> str | None:
    if gender not in ALLOWED_GENDERS:
        return "Please select Male or Female."
    return None


def validate_phone(phone: str) -> str | None:
    if not normalize_phone(phone):
        return "Enter a valid 10-digit Indian mobile number."
    return None


def validate_locations(source: str, destination: str) -> str | None:
    if not source:
        return "Pickup location is required."
    if not destination:
        return "Destination is required."
    if len(source) > MAX_LOCATION_LEN or len(destination) > MAX_LOCATION_LEN:
        return f"Locations must be at most {MAX_LOCATION_LEN} characters."
    if source.casefold() == destination.casefold():
        return "Pickup and destination must be different."
    return None


def validate_fare(total_fare: int) -> str | None:
    if total_fare < MIN_FARE:
        return f"Fare must be at least ₹{MIN_FARE}."
    if total_fare > MAX_FARE:
        return f"Fare cannot exceed ₹{MAX_FARE}."
    return None


def user_active_pools(db: Session, student: models.Student, now: datetime | None = None):
    """Future pools the student created, joined, or has a pending request on."""
    now = now or datetime.now()
    created = (
        db.query(models.Pool)
        .filter(
            models.Pool.creator_id == student.id,
            models.Pool.departure_time > now,
        )
        .all()
    )
    joined = (
        db.query(models.Pool)
        .filter(
            models.Pool.departure_time > now,
            models.Pool.members.any(models.Student.id == student.id),
        )
        .all()
    )
    pending = (
        db.query(models.Pool)
        .join(models.JoinRequest)
        .filter(
            models.JoinRequest.student_id == student.id,
            models.JoinRequest.status == "pending",
            models.Pool.departure_time > now,
        )
        .all()
    )
    by_id = {p.id: p for p in created}
    for p in joined:
        by_id[p.id] = p
    for p in pending:
        by_id[p.id] = p
    return list(by_id.values())


def has_schedule_conflict(
    db: Session,
    student: models.Student,
    departure: datetime,
    exclude_pool_id: int | None = None,
) -> models.Pool | None:
    for pool in user_active_pools(db, student, now=datetime.now()):
        if exclude_pool_id is not None and pool.id == exclude_pool_id:
            continue
        delta = abs(pool.departure_time - departure)
        if delta <= CONFLICT_WINDOW:
            return pool
    return None
