from datetime import datetime, timedelta
from urllib.parse import quote_plus

from dotenv import load_dotenv
from fastapi import FastAPI, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

from database import engine, Base, get_db
import models
from auth import (
    SECRET_KEY,
    OTP_MAX_ATTEMPTS,
    NotAuthenticated,
    can_resend_otp,
    create_otp_challenge,
    create_session,
    get_current_user,
    get_optional_user,
    latest_challenge,
    logout_all_devices,
    logout_current,
    set_session_cookie,
    verify_otp,
)
from otp_queue import enqueue_otp_email, ensure_worker_started
from rate_limit import RATE_LIMIT_MESSAGE, check_rate_limit
from validation import (
    HISTORY_RETENTION,
    can_relate_to_ride,
    normalize_email,
    normalize_gender,
    normalize_phone,
    normalize_username,
    validate_departure_window,
    validate_email,
    validate_fare,
    validate_gender,
    validate_locations,
    validate_phone,
    validate_username,
)

Base.metadata.create_all(bind=engine)

app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="autoshare_session",
    same_site="lax",
    https_only=False,
    max_age=60 * 60 * 24 * 365,
)
templates = Jinja2Templates(directory="templates")


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return RedirectResponse(url="/login", status_code=303)


def redirect_home(message=None, error=None) -> RedirectResponse:
    params = []
    if message:
        params.append(f"msg={quote_plus(message)}")
    if error:
        params.append(f"err={quote_plus(error)}")
    query = ("?" + "&".join(params)) if params else ""
    return RedirectResponse(url="/" + query, status_code=303)


def create_error(request: Request, current_user, error: str):
    now = datetime.now()
    return templates.TemplateResponse(
        "create.html",
        {
            "request": request,
            "current_user": current_user,
            "error": error,
            "min_dt": now.strftime("%Y-%m-%dT%H:%M"),
            "max_dt": (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M"),
        },
        status_code=400,
    )


def register_error(request: Request, error: str, info=None, **fields):
    return templates.TemplateResponse(
        "register.html",
        {"request": request, "error": error, "info": info, **fields},
        status_code=400,
    )


def login_error(request: Request, error: str, info=None, **fields):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": error, "info": info, **fields},
        status_code=400,
    )


def verify_page(
    request: Request,
    email: str,
    purpose: str,
    error=None,
    status_code=200,
    email_sent=False,
    email_failed=False,
    email_pending=True,
):
    return templates.TemplateResponse(
        "verify.html",
        {
            "request": request,
            "email": email,
            "purpose": purpose,
            "error": error,
            "email_sent": email_sent,
            "email_failed": email_failed,
            "email_pending": email_pending,
        },
        status_code=status_code,
    )


def verify_with_status(
    request: Request,
    db: Session,
    email: str,
    purpose: str,
    **kwargs,
):
    challenge = latest_challenge(db, email)
    if challenge and challenge.purpose != purpose:
        challenge = None
    sent, failed, pending = otp_send_status(challenge)
    return verify_page(
        request,
        email,
        purpose,
        email_sent=sent,
        email_failed=failed,
        email_pending=pending,
        **kwargs,
    )


def joiner_count(db: Session, pool_id: int) -> int:
    return (
        db.query(func.count())
        .select_from(models.PoolMembership)
        .filter(
            models.PoolMembership.pool_id == pool_id,
            models.PoolMembership.left_at.is_(None),
        )
        .scalar()
        or 0
    )


def lock_pool_row(db: Session, pool_id: int) -> models.Pool | None:
    db.execute(text("UPDATE pools SET id = id WHERE id = :pid"), {"pid": pool_id})
    return db.query(models.Pool).filter(models.Pool.id == pool_id).first()


def create_and_enqueue_otp(
    db: Session, email: str, purpose: str, phone=None, gender=None, username=None
):
    challenge, otp = create_otp_challenge(
        db,
        email=email,
        purpose=purpose,
        phone=phone,
        gender=gender,
        username=username,
    )
    enqueue_otp_email(challenge.id, email, otp)
    return challenge


def otp_send_status(challenge: models.OtpChallenge | None) -> tuple[bool, bool, bool]:
    """Return (sent, failed, pending)."""
    if not challenge:
        return False, False, True
    if challenge.email_send_failed:
        return False, True, False
    if challenge.email_sent_at:
        return True, False, False
    return False, False, True


def user_is_active_member(pool: models.Pool, student_id: int) -> bool:
    return any(m.student_id == student_id and m.left_at is None for m in pool.memberships)


def serialize_pool(pool: models.Pool, current_user_id: int) -> dict:
    return {
        "id": pool.id,
        "source": pool.source,
        "destination": pool.destination,
        "departure_time": pool.departure_time.isoformat(timespec="minutes"),
        "departure_display": pool.departure_time.strftime("%d %b %I:%M %p"),
        "total_fare": pool.total_fare,
        "fare_per_head": pool.fare_per_head,
        "remaining_seats": pool.remaining_seats,
        "headcount": pool.headcount,
        "max_capacity": pool.max_capacity,
        "is_host": pool.creator_id == current_user_id,
        "host_username": pool.creator.username,
        "pending_requests": [
            {
                "id": r.id,
                "username": r.student.username,
                "gender": r.student.gender_label,
                "phone": r.student.phone,
            }
            for r in pool.pending_requests
        ],
        "participants": [
            {
                "gender": p.gender_label,
                "phone": p.phone,
                "is_host": p.id == pool.creator_id,
                "is_you": p.id == current_user_id,
            }
            for p in pool.participants
        ],
    }


def serialize_history(pool: models.Pool, current_user_id: int) -> dict:
    role = "Hosted" if pool.creator_id == current_user_id else "Joined"
    return {
        "id": pool.id,
        "source": pool.source,
        "destination": pool.destination,
        "departure_display": pool.departure_time.strftime("%d %b %I:%M %p"),
        "fare_per_head": pool.fare_per_head,
        "role": role,
        "host_username": pool.creator.username,
        "end_reason": pool.end_reason,
        "ended_display": pool.ended_at.strftime("%d %b %I:%M %p"),
    }


def build_dashboard_data(db: Session, current_user: models.Student):
    now = datetime.now()
    history_cutoff = now - HISTORY_RETENTION

    active_pools = (
        db.query(models.Pool)
        .options(
            joinedload(models.Pool.creator),
            joinedload(models.Pool.memberships).joinedload(models.PoolMembership.student),
            joinedload(models.Pool.join_requests).joinedload(models.JoinRequest.student),
        )
        .filter(
            models.Pool.departure_time > now,
            models.Pool.cancelled_at.is_(None),
        )
        .order_by(models.Pool.departure_time.asc())
        .all()
    )

    my_rides = [
        p
        for p in active_pools
        if p.creator_id == current_user.id or user_is_active_member(p, current_user.id)
    ]
    my_ids = {p.id for p in my_rides}

    pending_request_pool_ids = {
        r.pool_id
        for r in db.query(models.JoinRequest)
        .join(models.Pool)
        .filter(
            models.JoinRequest.student_id == current_user.id,
            models.JoinRequest.status == "pending",
            models.Pool.departure_time > now,
            models.Pool.cancelled_at.is_(None),
        )
        .all()
    }

    upcoming = []
    for p in active_pools:
        if p.id in my_ids:
            continue
        # Show open rides, and rides where this user already has a pending request
        if p.remaining_seats > 0 or p.id in pending_request_pool_ids:
            upcoming.append(p)

    # History: ended within 7 days, user was creator or ever a member
    history_q = (
        db.query(models.Pool)
        .options(joinedload(models.Pool.memberships), joinedload(models.Pool.creator))
        .filter(
            or_(
                models.Pool.creator_id == current_user.id,
                models.Pool.memberships.any(
                    models.PoolMembership.student_id == current_user.id
                ),
            ),
            or_(
                models.Pool.cancelled_at.isnot(None),
                models.Pool.departure_time <= now,
            ),
        )
        .order_by(models.Pool.departure_time.desc())
        .all()
    )
    history = [p for p in history_q if p.ended_at >= history_cutoff]

    return {
        "my_rides": my_rides,
        "upcoming": upcoming,
        "history": history,
        "pending_request_pool_ids": pending_request_pool_ids,
    }


# --- Static / about ---


@app.get("/about")
def about_page(request: Request, current_user=Depends(get_optional_user)):
    return templates.TemplateResponse(
        "about.html",
        {"request": request, "current_user": current_user, "is_landing": False},
    )


# --- Auth ---


@app.on_event("startup")
def startup_otp_worker():
    ensure_worker_started()


@app.get("/register")
def register_page(
    request: Request,
    email: str = "",
    info: str = "",
    current_user=Depends(get_optional_user),
):
    if current_user:
        return RedirectResponse(url="/", status_code=303)
    email_norm = normalize_email(email) if email else ""
    return templates.TemplateResponse(
        "register.html",
        {
            "request": request,
            "error": None,
            "info": info or None,
            "email": email_norm or None,
        },
    )


@app.post("/register")
def handle_register(
    request: Request,
    email: str = Form(...),
    username: str = Form(...),
    gender: str = Form(...),
    phone: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_optional_user),
):
    if current_user:
        return RedirectResponse(url="/", status_code=303)

    email_norm = normalize_email(email)
    username_norm = normalize_username(username)
    gender_norm = normalize_gender(gender)
    phone_raw = phone.strip()
    phone_norm = normalize_phone(phone_raw)

    form_fields = {
        "email": email_norm,
        "username": username_norm,
        "gender": gender_norm,
        "phone": phone_raw,
    }

    rate_err = check_rate_limit(
        request, email=email_norm, ip_limit=10, ip_window_min=15, email_limit=5, email_window_min=15
    )
    if rate_err:
        return register_error(request, rate_err, **form_fields)

    error = (
        validate_email(email_norm)
        or validate_username(username_norm, email_norm)
        or validate_gender(gender_norm)
        or validate_phone(phone_raw)
    )
    if error:
        return register_error(request, error, **form_fields)

    if db.query(models.Student).filter_by(email=email_norm).first():
        return RedirectResponse(
            url=f"/login?email={quote_plus(email_norm)}&info=has_account",
            status_code=303,
        )
    if db.query(models.Student).filter_by(username=username_norm).first():
        return register_error(request, "That username is already taken.", **form_fields)

    existing = latest_challenge(db, email_norm)
    if existing and not can_resend_otp(existing):
        return RedirectResponse(
            url=f"/verify?email={quote_plus(email_norm)}&purpose=signup",
            status_code=303,
        )

    create_and_enqueue_otp(
        db,
        email_norm,
        purpose="signup",
        phone=phone_norm,
        gender=gender_norm,
        username=username_norm,
    )

    return RedirectResponse(
        url=f"/verify?email={quote_plus(email_norm)}&purpose=signup", status_code=303
    )


@app.get("/login")
def login_page(
    request: Request,
    email: str = "",
    info: str = "",
    current_user=Depends(get_optional_user),
):
    if current_user:
        return RedirectResponse(url="/", status_code=303)
    email_norm = normalize_email(email) if email else ""
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": None,
            "info": info or None,
            "email": email_norm or None,
        },
    )


@app.post("/login")
def handle_login(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_optional_user),
):
    if current_user:
        return RedirectResponse(url="/", status_code=303)

    email_norm = normalize_email(email)
    error = validate_email(email_norm)
    if error:
        return login_error(request, error, email=email_norm)

    rate_err = check_rate_limit(
        request, email=email_norm, ip_limit=10, ip_window_min=15, email_limit=5, email_window_min=15
    )
    if rate_err:
        return login_error(request, rate_err, email=email_norm)

    student = db.query(models.Student).filter_by(email=email_norm).first()
    if not student:
        return RedirectResponse(
            url=f"/register?email={quote_plus(email_norm)}&info=no_account",
            status_code=303,
        )

    existing = latest_challenge(db, email_norm)
    if existing and not can_resend_otp(existing):
        return RedirectResponse(
            url=f"/verify?email={quote_plus(email_norm)}&purpose=login",
            status_code=303,
        )

    create_and_enqueue_otp(db, email_norm, purpose="login")

    return RedirectResponse(
        url=f"/verify?email={quote_plus(email_norm)}&purpose=login", status_code=303
    )


@app.get("/verify")
def verify_otp_page(
    request: Request,
    email: str = "",
    purpose: str = "login",
    db: Session = Depends(get_db),
    current_user=Depends(get_optional_user),
):
    if current_user:
        return RedirectResponse(url="/", status_code=303)
    email_norm = normalize_email(email)
    if validate_email(email_norm) or purpose not in ("signup", "login"):
        return RedirectResponse(url="/login", status_code=303)
    challenge = latest_challenge(db, email_norm)
    if challenge and challenge.purpose != purpose:
        challenge = None
    sent, failed, pending = otp_send_status(challenge)
    return verify_page(
        request,
        email_norm,
        purpose,
        email_sent=sent,
        email_failed=failed,
        email_pending=pending,
    )


@app.get("/api/otp-status")
def otp_status_api(
    email: str = "",
    purpose: str = "login",
    db: Session = Depends(get_db),
):
    email_norm = normalize_email(email)
    if validate_email(email_norm) or purpose not in ("signup", "login"):
        return JSONResponse({"detail": "Invalid request"}, status_code=400)
    challenge = latest_challenge(db, email_norm)
    if challenge and challenge.purpose != purpose:
        challenge = None
    sent, failed, pending = otp_send_status(challenge)
    return {"sent": sent, "failed": failed, "pending": pending}


@app.post("/verify-otp")
def handle_verify_otp(
    request: Request,
    email: str = Form(...),
    purpose: str = Form(...),
    otp: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_optional_user),
):
    if current_user:
        return RedirectResponse(url="/", status_code=303)

    email_norm = normalize_email(email)
    otp_code = otp.strip()

    if validate_email(email_norm) or purpose not in ("signup", "login"):
        return RedirectResponse(url="/login", status_code=303)

    rate_err = check_rate_limit(
        request, email=email_norm, ip_limit=30, ip_window_min=15, email_limit=None, email_window_min=None
    )
    if rate_err:
        return verify_with_status(
            request, db, email_norm, purpose, error=rate_err, status_code=429
        )

    challenge = latest_challenge(db, email_norm)
    if (
        not challenge
        or challenge.purpose != purpose
        or challenge.expires_at <= datetime.now()
    ):
        return verify_with_status(
            request,
            db,
            email_norm,
            purpose,
            error="Code expired or missing. Request a new one.",
            status_code=400,
        )

    if challenge.attempts >= OTP_MAX_ATTEMPTS:
        return verify_with_status(
            request,
            db,
            email_norm,
            purpose,
            error="Too many attempts. Request a new code.",
            status_code=400,
        )

    if not verify_otp(otp_code, challenge.otp_hash):
        challenge.attempts += 1
        db.commit()
        return verify_with_status(
            request,
            db,
            email_norm,
            purpose,
            error="Invalid code. Please try again.",
            status_code=400,
        )

    phone = challenge.phone
    gender = challenge.gender
    username = challenge.username
    db.delete(challenge)
    db.commit()

    if purpose == "signup":
        if db.query(models.Student).filter_by(email=email_norm).first():
            return verify_page(
                request,
                email_norm,
                purpose,
                error="An account with this email already exists. Please log in.",
                status_code=400,
            )
        if not phone or not gender or not username:
            return verify_page(
                request,
                email_norm,
                purpose,
                error="Signup data missing. Please register again.",
                status_code=400,
            )
        student = models.Student(
            email=email_norm,
            username=username,
            phone=phone,
            gender=gender,
        )
        db.add(student)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return verify_page(
                request,
                email_norm,
                purpose,
                error="Email or username already taken. Please register again.",
                status_code=400,
            )
        db.refresh(student)
    else:
        student = db.query(models.Student).filter_by(email=email_norm).first()
        if not student:
            return verify_page(
                request,
                email_norm,
                purpose,
                error="Account not found. Please register first.",
                status_code=400,
            )

    token = create_session(db, student.id)
    set_session_cookie(request, token)
    return RedirectResponse(url="/", status_code=303)


@app.post("/resend-otp")
def handle_resend_otp(
    request: Request,
    email: str = Form(...),
    purpose: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_optional_user),
):
    if current_user:
        return RedirectResponse(url="/", status_code=303)

    email_norm = normalize_email(email)
    if validate_email(email_norm) or purpose not in ("signup", "login"):
        return RedirectResponse(url="/login", status_code=303)

    rate_err = check_rate_limit(
        request, email=email_norm, ip_limit=10, ip_window_min=15, email_limit=3, email_window_min=15
    )
    if rate_err:
        return verify_with_status(
            request, db, email_norm, purpose, error=rate_err, status_code=429
        )

    existing = latest_challenge(db, email_norm)
    if existing and not can_resend_otp(existing):
        return verify_with_status(
            request,
            db,
            email_norm,
            purpose,
            error="Please wait a minute before requesting another code.",
            status_code=400,
        )

    phone = existing.phone if existing and existing.purpose == "signup" else None
    gender = existing.gender if existing and existing.purpose == "signup" else None
    username = existing.username if existing and existing.purpose == "signup" else None

    if purpose == "signup":
        if db.query(models.Student).filter_by(email=email_norm).first():
            return RedirectResponse(
                url=f"/login?email={quote_plus(email_norm)}&info=has_account",
                status_code=303,
            )
        if not phone or not gender or not username:
            return RedirectResponse(url="/register", status_code=303)
    else:
        if not db.query(models.Student).filter_by(email=email_norm).first():
            return RedirectResponse(
                url=f"/register?email={quote_plus(email_norm)}&info=no_account",
                status_code=303,
            )

    create_and_enqueue_otp(
        db,
        email_norm,
        purpose=purpose,
        phone=phone,
        gender=gender,
        username=username,
    )

    return verify_page(
        request,
        email_norm,
        purpose,
        error=None,
        email_sent=False,
        email_failed=False,
        email_pending=True,
    )


@app.post("/logout")
def handle_logout(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    logout_current(request, db)
    return RedirectResponse(url="/", status_code=303)


@app.post("/logout-all")
def handle_logout_all(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    logout_all_devices(request, db, current_user.id)
    return RedirectResponse(url="/", status_code=303)


# --- Dashboard ---


@app.get("/")
def home(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_optional_user),
):
    # Guests land on About; logged-in users see the dashboard
    if not current_user:
        return templates.TemplateResponse(
            "about.html",
            {"request": request, "current_user": None, "is_landing": True},
        )

    data = build_dashboard_data(db, current_user)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "pools": data["upcoming"],
            "my_rides": data["my_rides"],
            "history": data["history"],
            "pending_request_pool_ids": data["pending_request_pool_ids"],
            "current_user": current_user,
            "msg": request.query_params.get("msg"),
            "err": request.query_params.get("err"),
        },
    )


@app.get("/api/dashboard")
def api_dashboard(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    data = build_dashboard_data(db, current_user)
    return {
        "my_rides": [serialize_pool(p, current_user.id) for p in data["my_rides"]],
        "upcoming": [serialize_pool(p, current_user.id) for p in data["upcoming"]],
        "history": [serialize_history(p, current_user.id) for p in data["history"]],
        "pending_request_pool_ids": list(data["pending_request_pool_ids"]),
    }


@app.get("/pool/create")
def create_pool_page(request: Request, current_user=Depends(get_current_user)):
    now = datetime.now()
    return templates.TemplateResponse(
        "create.html",
        {
            "request": request,
            "current_user": current_user,
            "error": None,
            "min_dt": now.strftime("%Y-%m-%dT%H:%M"),
            "max_dt": (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M"),
        },
    )


@app.post("/pool/create")
def handle_create_pool(
    request: Request,
    source: str = Form(...),
    destination: str = Form(...),
    departure_str: str = Form(...),
    total_fare: int = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    now = datetime.now()
    try:
        departure = datetime.fromisoformat(departure_str)
    except ValueError:
        return create_error(request, current_user, "Invalid departure date/time.")

    window_err = validate_departure_window(departure, now)
    if window_err:
        return create_error(request, current_user, window_err)

    src = source.strip()
    dest = destination.strip()
    loc_err = validate_locations(src, dest)
    if loc_err:
        return create_error(request, current_user, loc_err)

    fare_err = validate_fare(total_fare)
    if fare_err:
        return create_error(request, current_user, fare_err)

    relate_err = can_relate_to_ride(db, current_user, departure)
    if relate_err:
        return create_error(request, current_user, relate_err)

    new_pool = models.Pool(
        creator_id=current_user.id,
        source=src,
        destination=dest,
        departure_time=departure,
        total_fare=total_fare,
        max_capacity=4,
    )
    db.add(new_pool)
    db.commit()
    return redirect_home(message="Ride offered.")


@app.post("/pool/{pool_id}/request")
def request_join(
    pool_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    pool = lock_pool_row(db, pool_id)

    if (
        not pool
        or pool.cancelled_at
        or pool.departure_time <= datetime.now()
    ):
        db.rollback()
        return redirect_home(error="This ride is no longer available.")

    if pool.creator_id == current_user.id:
        db.rollback()
        return redirect_home(error="You already own this ride.")

    active_membership = (
        db.query(models.PoolMembership)
        .filter_by(pool_id=pool.id, student_id=current_user.id)
        .filter(models.PoolMembership.left_at.is_(None))
        .first()
    )
    if active_membership:
        db.rollback()
        return redirect_home(error="You are already in this ride.")

    if 1 + joiner_count(db, pool.id) >= pool.max_capacity:
        db.rollback()
        return redirect_home(error="This ride is full.")

    existing = (
        db.query(models.JoinRequest)
        .filter_by(pool_id=pool.id, student_id=current_user.id, status="pending")
        .first()
    )
    if existing:
        db.rollback()
        return redirect_home(error="You already have a pending request for this ride.")

    relate_err = can_relate_to_ride(
        db, current_user, pool.departure_time, exclude_pool_id=pool.id
    )
    if relate_err:
        db.rollback()
        return redirect_home(error=relate_err)

    db.add(
        models.JoinRequest(
            pool_id=pool.id,
            student_id=current_user.id,
            status="pending",
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return redirect_home(error="Could not send join request. Please try again.")
    return redirect_home(message="Join request sent. Waiting for host approval.")


@app.post("/pool/{pool_id}/request/{request_id}/approve")
def approve_request(
    pool_id: int,
    request_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    pool = lock_pool_row(db, pool_id)

    if not pool or pool.cancelled_at or pool.departure_time <= datetime.now():
        db.rollback()
        return redirect_home(error="This ride is no longer available.")

    if pool.creator_id != current_user.id:
        db.rollback()
        return redirect_home(error="Only the host can approve join requests.")

    join_req = (
        db.query(models.JoinRequest)
        .filter_by(id=request_id, pool_id=pool.id, status="pending")
        .first()
    )
    if not join_req:
        db.rollback()
        return redirect_home(error="Join request not found.")

    requester = db.query(models.Student).filter_by(id=join_req.student_id).first()
    if not requester:
        db.rollback()
        return redirect_home(error="Requester account not found.")

    if 1 + joiner_count(db, pool.id) >= pool.max_capacity:
        db.rollback()
        return redirect_home(error="This ride is full. Reject extra requests.")

    membership = (
        db.query(models.PoolMembership)
        .filter_by(pool_id=pool.id, student_id=requester.id)
        .first()
    )
    if membership and membership.left_at is None:
        db.delete(join_req)
        db.commit()
        return redirect_home(message="Already in the pool.")

    relate_err = can_relate_to_ride(
        db, requester, pool.departure_time, exclude_pool_id=pool.id
    )
    if relate_err:
        db.rollback()
        return redirect_home(error=f"Cannot approve: {relate_err}")

    if membership:
        membership.left_at = None
        membership.joined_at = datetime.now()
    else:
        db.add(
            models.PoolMembership(
                pool_id=pool.id,
                student_id=requester.id,
                joined_at=datetime.now(),
            )
        )
    db.delete(join_req)
    db.flush()

    if 1 + joiner_count(db, pool.id) > pool.max_capacity:
        db.rollback()
        return redirect_home(error="This ride is full.")

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return redirect_home(error="Could not approve request. Please try again.")
    return redirect_home(message="Request approved. Rider added to the pool.")


@app.post("/pool/{pool_id}/request/{request_id}/reject")
def reject_request(
    pool_id: int,
    request_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    pool = lock_pool_row(db, pool_id)

    if not pool:
        db.rollback()
        return redirect_home(error="Ride not found.")

    if pool.creator_id != current_user.id:
        db.rollback()
        return redirect_home(error="Only the host can reject join requests.")

    join_req = (
        db.query(models.JoinRequest)
        .filter_by(id=request_id, pool_id=pool.id, status="pending")
        .first()
    )
    if not join_req:
        db.rollback()
        return redirect_home(error="Join request not found.")

    db.delete(join_req)
    db.commit()
    return redirect_home(message="Join request rejected.")


@app.post("/pool/{pool_id}/request/cancel")
def cancel_request(
    pool_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    join_req = (
        db.query(models.JoinRequest)
        .filter_by(pool_id=pool_id, student_id=current_user.id, status="pending")
        .first()
    )
    if not join_req:
        return redirect_home(error="No pending request to cancel.")

    db.delete(join_req)
    db.commit()
    return redirect_home(message="Join request cancelled.")


@app.post("/pool/{pool_id}/leave")
def leave_pool(
    pool_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    pool = lock_pool_row(db, pool_id)

    if not pool or pool.cancelled_at or pool.departure_time <= datetime.now():
        db.rollback()
        return redirect_home(error="This ride is no longer available.")

    if pool.creator_id == current_user.id:
        db.rollback()
        return redirect_home(error="Creators cannot leave — delete the ride instead.")

    membership = (
        db.query(models.PoolMembership)
        .filter_by(pool_id=pool.id, student_id=current_user.id)
        .filter(models.PoolMembership.left_at.is_(None))
        .first()
    )
    if membership is None:
        db.rollback()
        return redirect_home(error="You are not in this ride.")

    membership.left_at = datetime.now()
    db.commit()
    return redirect_home(message="Left the ride.")


@app.post("/pool/{pool_id}/delete")
def delete_pool(
    pool_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    pool = lock_pool_row(db, pool_id)

    if not pool:
        db.rollback()
        return redirect_home(error="Ride not found.")

    if pool.creator_id != current_user.id:
        db.rollback()
        return redirect_home(error="You can only delete rides you created.")

    if pool.cancelled_at:
        db.rollback()
        return redirect_home(error="This ride is already cancelled.")

    if pool.departure_time <= datetime.now():
        db.rollback()
        return redirect_home(error="This ride has already departed.")

    pool.cancelled_at = datetime.now()
    # Clear pending requests
    for req in list(pool.pending_requests):
        db.delete(req)
    db.commit()
    return redirect_home(message="Ride cancelled.")


app.mount("/static", StaticFiles(directory="static"), name="static")
