# AutoShare

Campus auto-rickshaw ride pooling for college students (IIIT Kottayam). Students offer or request shared autos so they do not wait alone. Autos have a fixed fare (max 4 riders); pooling cuts per-head cost.

## Stack

- **Backend:** FastAPI + Uvicorn
- **Templates:** Jinja2 (server-rendered)
- **DB:** SQLite via SQLAlchemy (`autoshare.db`)
- **Auth:** College email + OTP (Gmail SMTP) + opaque session tokens in DB (1 year)
- **CSS:** Tailwind CDN

## Setup

```powershell
cd c:\Users\sarth\autoshare
.\venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` with your secrets (see **Gmail App Password** below).

```powershell
.\venv\Scripts\python.exe -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

- PC: http://127.0.0.1:8000  
- Phone (same Wi‑Fi): `http://<LAN-IP>:8000`

After model changes, delete `autoshare.db` once so tables recreate (no Alembic yet).

## Gmail App Password (free OTP email)

OTP emails are sent with Gmail SMTP (free). Sender defaults to `if.not.me.then.who25@gmail.com` via `SMTP_EMAIL` in `.env`.

1. Sign in to that Google account.
2. Enable **2-Step Verification**.
3. Open [Google App Passwords](https://myaccount.google.com/apppasswords) → create a password for **Mail**.
4. Put the 16-character password in `.env` as `SMTP_APP_PASSWORD` (no spaces).
5. Set a long random `AUTOSHARE_SECRET_KEY`.

Outgoing mail uses From display name **AutoShare (no-reply)** and asks recipients not to reply.

**Never commit `.env`.** Only `.env.example` is tracked.

## Authentication

- **Register:** `@iiitkottayam.ac.in` email + gender + phone → OTP email → verify → logged in for **1 year**
- **Login:** email only → OTP → same session lifetime
- OTP: 6 digits, hashed in DB, **10 min** expiry, **5** attempts, **60s** resend cooldown
- Cookie stores an opaque `session_token`; rows live in `sessions` table
- **Logout:** ends this device
- **Logout all:** deletes all sessions for the user (every device)

## Product rules

1. Rides are **today only** (date + departure time).
2. Max **4** riders (host + up to 3 approved joiners). Full rides leave the public list.
3. Per-head fare = `total_fare / headcount`.
4. After departure time, rides disappear from the dashboard.
5. Host must **approve** join requests; requests show **phone + gender** only.
6. **Private pool contacts** (members only): phone + gender only — no email on cards.
7. Public Upcoming cards do **not** show who created the ride.
8. **45-minute** schedule conflict window across offer / pending request / membership.

## Main routes

| Method | Path | Purpose |
|--------|------|---------|
| GET/POST | `/register` | Signup + send OTP |
| GET/POST | `/login` | Login + send OTP |
| GET/POST | `/verify`, `/verify-otp`, `/resend-otp` | OTP verification |
| POST | `/logout`, `/logout-all` | End session(s) |
| GET | `/` | Dashboard |
| GET/POST | `/pool/create` | Offer a ride |
| POST | `/pool/{id}/request` | Request to join |
| POST | `/pool/{id}/request/{rid}/approve\|reject` | Host decision |
| POST | `/pool/{id}/request/cancel` | Cancel own request |
| POST | `/pool/{id}/leave` | Leave pool |
| POST | `/pool/{id}/delete` | Host deletes ride |

## Data model

- **students** — `email`, `gender`, `phone`
- **pools** — route, time, fare, capacity, `creator_id`
- **pool_members** — approved joiners
- **join_requests** — pending join requests
- **otp_challenges** — hashed OTP + signup phone/gender payload
- **sessions** — opaque tokens, expiry

## Project layout

```
autoshare/
├── main.py
├── auth.py
├── email_service.py
├── models.py
├── database.py
├── validation.py
├── requirements.txt
├── .env.example
├── templates/
└── README.md
```

## Security notes

- Only `@iiitkottayam.ac.in` emails can register/login.
- OTP never stored in plaintext; App Password stays in `.env`.
- Host-only approve/reject/delete; contacts limited as above.
- Production: strong `AUTOSHARE_SECRET_KEY`, HTTPS, set session `https_only=True`.
