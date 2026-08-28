"""Shared ride / account validation helpers."""

import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

import models

MAX_LOCATION_LEN = 100
MAX_FARE = 10_000
MIN_FARE = 1
MIN_USERNAME_LEN = 3
MAX_USERNAME_LEN = 32
PHONE_PATTERN = re.compile(r"^(?:\+91[\-\s]?|0)?([6-9]\d{9})$")
EMAIL_PATTERN = re.compile(r"^[a-z0-9._%+-]+@iiitkottayam\.ac\.in$")
USERNAME_PATTERN = re.compile(r"^[a-z0-9_]{3,32}$")
ALLOWED_GENDERS = {"male", "female"}
MIN_RIDE_GAP = timedelta(minutes=30)
MAX_ACTIVE_RIDES = 2
BOOKING_WINDOW = timedelta(hours=24)
HISTORY_RETENTION = timedelta(days=7)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def normalize_phone(phone: str) -> str:
    match = PHONE_PATTERN.fullmatch(phone.strip().replace(" ", ""))
    return match.group(1) if match else ""


def normalize_gender(gender: str) -> str:
    return gender.strip().lower()


def normalize_username(username: str) -> str:
    return username.strip().lower()


def alnum_only(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


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


def validate_username(username: str, email: str) -> str | None:
    if not USERNAME_PATTERN.fullmatch(username):
        return (
            f"Username must be {MIN_USERNAME_LEN}–{MAX_USERNAME_LEN} characters "
            "(lowercase letters, numbers, underscore)."
        )
    local = email.split("@", 1)[0]
    if alnum_only(username) not in alnum_only(local):
        return "Username should roughly match your college email (e.g. part of your email name)."
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


def validate_departure_window(departure: datetime, now: datetime | None = None) -> str | None:
    now = now or datetime.now()
    if departure <= now:
        return "Departure must be in the future."
    if departure > now + BOOKING_WINDOW:
        return "You can only book within the next 24 hours."
    return None


def user_active_pools(db: Session, student: models.Student, now: datetime | None = None):
    """Active (not cancelled, not departed) pools the student created, joined, or requested."""
    now = now or datetime.now()
    created = (
        db.query(models.Pool)
        .filter(
            models.Pool.creator_id == student.id,
            models.Pool.departure_time > now,
            models.Pool.cancelled_at.is_(None),
        )
        .all()
    )
    joined = (
        db.query(models.Pool)
        .join(models.PoolMembership)
        .filter(
            models.PoolMembership.student_id == student.id,
            models.PoolMembership.left_at.is_(None),
            models.Pool.departure_time > now,
            models.Pool.cancelled_at.is_(None),
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
            models.Pool.cancelled_at.is_(None),
        )
        .all()
    )
    by_id = {p.id: p for p in created}
    for p in joined:
        by_id[p.id] = p
    for p in pending:
        by_id[p.id] = p
    return list(by_id.values())


def can_relate_to_ride(
    db: Session,
    student: models.Student,
    departure: datetime,
    exclude_pool_id: int | None = None,
) -> str | None:
    """
    Enforce max 2 active ride relationships and >= 30 minute gap vs other active rides.
    Returns an error message or None if allowed.
    """
    active = user_active_pools(db, student)
    others = [p for p in active if exclude_pool_id is None or p.id != exclude_pool_id]

    if len(others) >= MAX_ACTIVE_RIDES:
        return "You can be related to at most 2 rides at a time (offer, join, or pending request)."

    for pool in others:
        if abs(pool.departure_time - departure) < MIN_RIDE_GAP:
            return (
                "Your rides must be at least 30 minutes apart from each other. "
                "Pick a different time or leave/cancel the other ride."
            )
    return None
