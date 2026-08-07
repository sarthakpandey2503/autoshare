from datetime import datetime, date, time
from urllib.parse import quote_plus

from dotenv import load_dotenv
from fastapi import FastAPI, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
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
from email_service import EmailSendError, send_otp_email
from validation import (
    has_schedule_conflict,
    normalize_email,
    normalize_gender,
    normalize_phone,
    validate_email,
    validate_fare,
    validate_gender,
    validate_locations,
    validate_phone,
)

Base.metadata.create_all(bind=engine)

app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="autoshare_session",
    same_site="lax",
    https_only=False,  # set True behind HTTPS in production
    max_age=60 * 60 * 24 * 365,  # 1 year
)
templates = Jinja2Templates(directory="templates")


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
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
    return templates.TemplateResponse(
        "create.html",
        {"request": request, "current_user": current_user, "error": error},
        status_code=400,
    )


def register_error(request: Request, error: str, **fields):
    return templates.TemplateResponse(
        "register.html",
        {"request": request, "error": error, **fields},
        status_code=400,
    )


def login_error(request: Request, error: str, **fields):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": error, **fields},
        status_code=400,
    )


def verify_page(request: Request, email: str, purpose: str, error=None, status_code=200):
    return templates.TemplateResponse(
        "verify.html",
        {
            "request": request,
            "email": email,
            "purpose": purpose,
            "error": error,
        },
        status_code=status_code,
    )


def joiner_count(db: Session, pool_id: int) -> int:
    return (
        db.query(func.count())
        .select_from(models.pool_members)
        .filter(models.pool_members.c.pool_id == pool_id)
        .scalar()
        or 0
    )


def lock_pool_row(db: Session, pool_id: int) -> models.Pool | None:
    db.execute(text("UPDATE pools SET id = id WHERE id = :pid"), {"pid": pool_id})
    return db.query(models.Pool).filter(models.Pool.id == pool_id).first()


def send_challenge_email(db: Session, email: str, purpose: str, phone=None, gender=None):
    challenge, otp = create_otp_challenge(
        db, email=email, purpose=purpose, phone=phone, gender=gender
    )
    try:
        send_otp_email(email, otp)
    except EmailSendError:
        db.query(models.OtpChallenge).filter(models.OtpChallenge.id == challenge.id).delete()
        db.commit()
        raise
    return challenge


# --- Auth routes ---


@app.get("/register")
def register_page(request: Request, current_user=Depends(get_optional_user)):
    if current_user:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("register.html", {"request": request, "error": None})


@app.post("/register")
def handle_register(
    request: Request,
    email: str = Form(...),
    gender: str = Form(...),
    phone: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_optional_user),
):
    if current_user:
        return RedirectResponse(url="/", status_code=303)

    email_norm = normalize_email(email)
    gender_norm = normalize_gender(gender)
    phone_raw = phone.strip()
    phone_norm = normalize_phone(phone_raw)

    form_fields = {"email": email_norm, "gender": gender_norm, "phone": phone_raw}

    error = validate_email(email_norm) or validate_gender(gender_norm) or validate_phone(phone_raw)
    if error:
        return register_error(request, error, **form_fields)

    if db.query(models.Student).filter_by(email=email_norm).first():
        return register_error(
            request,
            "An account with this email already exists. Please log in.",
            **form_fields,
        )

    existing = latest_challenge(db, email_norm)
    if existing and not can_resend_otp(existing):
        return register_error(
            request,
            "Please wait a minute before requesting another code.",
            **form_fields,
        )

    try:
        send_challenge_email(
            db, email_norm, purpose="signup", phone=phone_norm, gender=gender_norm
        )
    except EmailSendError as exc:
        return register_error(request, str(exc), **form_fields)

    return RedirectResponse(url=f"/verify?email={quote_plus(email_norm)}&purpose=signup", status_code=303)


@app.get("/login")
def login_page(request: Request, current_user=Depends(get_optional_user)):
    if current_user:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


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

    student = db.query(models.Student).filter_by(email=email_norm).first()
    if not student:
        # Generic message — do not reveal whether the email is registered
        return login_error(
            request,
            "If this email is registered, a code will be sent. "
            "New users should create an account first.",
            email=email_norm,
        )

    existing = latest_challenge(db, email_norm)
    if existing and not can_resend_otp(existing):
        return RedirectResponse(
            url=f"/verify?email={quote_plus(email_norm)}&purpose=login",
            status_code=303,
        )

    try:
        send_challenge_email(db, email_norm, purpose="login")
    except EmailSendError as exc:
        return login_error(request, str(exc), email=email_norm)

    return RedirectResponse(url=f"/verify?email={quote_plus(email_norm)}&purpose=login", status_code=303)


@app.get("/verify")
def verify_otp_page(
    request: Request,
    email: str = "",
    purpose: str = "login",
    current_user=Depends(get_optional_user),
):
    if current_user:
        return RedirectResponse(url="/", status_code=303)
    email_norm = normalize_email(email)
    if validate_email(email_norm) or purpose not in ("signup", "login"):
        return RedirectResponse(url="/login", status_code=303)
    return verify_page(request, email_norm, purpose)


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

    challenge = latest_challenge(db, email_norm)
    if (
        not challenge
        or challenge.purpose != purpose
        or challenge.expires_at <= datetime.now()
    ):
        return verify_page(
            request,
            email_norm,
            purpose,
            error="Code expired or missing. Request a new one.",
            status_code=400,
        )

    if challenge.attempts >= OTP_MAX_ATTEMPTS:
        return verify_page(
            request,
            email_norm,
            purpose,
            error="Too many attempts. Request a new code.",
            status_code=400,
        )

    if not verify_otp(otp_code, challenge.otp_hash):
        challenge.attempts += 1
        db.commit()
        return verify_page(
            request,
            email_norm,
            purpose,
            error="Invalid code. Please try again.",
            status_code=400,
        )

    # Success — consume challenge
    phone = challenge.phone
    gender = challenge.gender
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
        if not phone or not gender:
            return verify_page(
                request,
                email_norm,
                purpose,
                error="Signup data missing. Please register again.",
                status_code=400,
            )
        student = models.Student(email=email_norm, phone=phone, gender=gender)
        db.add(student)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return verify_page(
                request,
                email_norm,
                purpose,
                error="An account with this email already exists. Please log in.",
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

    existing = latest_challenge(db, email_norm)
    if existing and not can_resend_otp(existing):
        return verify_page(
            request,
            email_norm,
            purpose,
            error="Please wait a minute before requesting another code.",
            status_code=400,
        )

    phone = existing.phone if existing and existing.purpose == "signup" else None
    gender = existing.gender if existing and existing.purpose == "signup" else None

    if purpose == "signup":
        if db.query(models.Student).filter_by(email=email_norm).first():
            return verify_page(
                request,
                email_norm,
                purpose,
                error="An account with this email already exists. Please log in.",
                status_code=400,
            )
        if not phone or not gender:
            return RedirectResponse(url="/register", status_code=303)
    else:
        if not db.query(models.Student).filter_by(email=email_norm).first():
            return verify_page(
                request,
                email_norm,
                purpose,
                error="Account not found. Please register first.",
                status_code=400,
            )

    try:
        send_challenge_email(db, email_norm, purpose=purpose, phone=phone, gender=gender)
    except EmailSendError as exc:
        return verify_page(request, email_norm, purpose, error=str(exc), status_code=400)

    return verify_page(request, email_norm, purpose, error=None)


@app.post("/logout")
def handle_logout(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    logout_current(request, db)
    return RedirectResponse(url="/login", status_code=303)


@app.post("/logout-all")
def handle_logout_all(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    logout_all_devices(request, db, current_user.id)
    return RedirectResponse(url="/login", status_code=303)


# --- Dashboard & rides ---


@app.get("/")
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    now = datetime.now()
    future_pools = (
        db.query(models.Pool)
        .options(
            joinedload(models.Pool.creator),
            joinedload(models.Pool.members),
            joinedload(models.Pool.join_requests).joinedload(models.JoinRequest.student),
        )
        .filter(models.Pool.departure_time > now)
        .order_by(models.Pool.departure_time.asc())
        .all()
    )

    my_rides = [
        pool
        for pool in future_pools
        if pool.creator_id == current_user.id or current_user in pool.members
    ]
    my_ride_ids = {pool.id for pool in my_rides}

    pending_request_pool_ids = {
        r.pool_id
        for r in db.query(models.JoinRequest)
        .filter(
            models.JoinRequest.student_id == current_user.id,
            models.JoinRequest.status == "pending",
        )
        .all()
    }

    upcoming = [
        pool
        for pool in future_pools
        if pool.remaining_seats > 0 and pool.id not in my_ride_ids
    ]

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "pools": upcoming,
            "my_rides": my_rides,
            "pending_request_pool_ids": pending_request_pool_ids,
            "current_user": current_user,
            "msg": request.query_params.get("msg"),
            "err": request.query_params.get("err"),
        },
    )


@app.get("/pool/create")
def create_pool_page(request: Request, current_user=Depends(get_current_user)):
    return templates.TemplateResponse(
        "create.html",
        {"request": request, "current_user": current_user, "error": None},
    )


@app.post("/pool/create")
def handle_create_pool(
    request: Request,
    source: str = Form(...),
    destination: str = Form(...),
    time_str: str = Form(...),
    total_fare: int = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    try:
        parsed_time = time.fromisoformat(time_str)
    except ValueError:
        return create_error(request, current_user, "Invalid departure time.")

    departure = datetime.combine(date.today(), parsed_time)
    if departure <= datetime.now():
        return create_error(
            request,
            current_user,
            "Cannot create a pool for a time that has already passed today.",
        )

    src = source.strip()
    dest = destination.strip()
    loc_err = validate_locations(src, dest)
    if loc_err:
        return create_error(request, current_user, loc_err)

    fare_err = validate_fare(total_fare)
    if fare_err:
        return create_error(request, current_user, fare_err)

    conflict = has_schedule_conflict(db, current_user, departure)
    if conflict:
        return create_error(
            request,
            current_user,
            "You already have another ride within 45 minutes of this time. "
            "Leave or delete it first.",
        )

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

    if not pool or pool.departure_time <= datetime.now():
        db.rollback()
        return redirect_home(error="This ride is no longer available.")

    if pool.creator_id == current_user.id:
        db.rollback()
        return redirect_home(error="You already own this ride.")

    already_in = (
        db.query(models.pool_members)
        .filter_by(pool_id=pool.id, student_id=current_user.id)
        .first()
    )
    if already_in:
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

    conflict = has_schedule_conflict(
        db, current_user, pool.departure_time, exclude_pool_id=pool.id
    )
    if conflict:
        db.rollback()
        return redirect_home(
            error="You already have another ride within 45 minutes of this one. "
            "Leave or cancel it first."
        )

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

    if not pool or pool.departure_time <= datetime.now():
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

    already_in = (
        db.query(models.pool_members)
        .filter_by(pool_id=pool.id, student_id=requester.id)
        .first()
    )
    if already_in:
        db.delete(join_req)
        db.commit()
        return redirect_home(message="Already in the pool.")

    conflict = has_schedule_conflict(
        db, requester, pool.departure_time, exclude_pool_id=pool.id
    )
    if conflict:
        db.rollback()
        return redirect_home(
            error="Cannot approve: requester has a conflicting ride. Ask them to leave it first."
        )

    pool.members.append(requester)
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

    if not pool or pool.departure_time <= datetime.now():
        db.rollback()
        return redirect_home(error="This ride is no longer available.")

    if pool.creator_id == current_user.id:
        db.rollback()
        return redirect_home(error="Creators cannot leave — delete the ride instead.")

    membership = (
        db.query(models.pool_members)
        .filter_by(pool_id=pool.id, student_id=current_user.id)
        .first()
    )
    if membership is None:
        db.rollback()
        return redirect_home(error="You are not in this ride.")

    db.execute(
        models.pool_members.delete().where(
            (models.pool_members.c.pool_id == pool.id)
            & (models.pool_members.c.student_id == current_user.id)
        )
    )
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

    if pool.departure_time <= datetime.now():
        db.rollback()
        return redirect_home(error="This ride has already departed.")

    pool.members.clear()
    db.delete(pool)
    db.commit()
    return redirect_home(message="Ride deleted.")
