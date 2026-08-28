# AutoShare

Campus auto-rickshaw ride pooling for college students (IIIT Kottayam). Students offer or request shared autos so nobody waits alone. Autos charge a fixed fare (max 4 riders); pooling cuts per-head cost.

## Stack

- **Backend:** FastAPI + Uvicorn
- **Templates:** Jinja2 (server-rendered) + light JS polling
- **DB:** SQLite via SQLAlchemy (`autoshare.db`)
- **Auth:** College email + OTP (Gmail SMTP) + opaque session tokens (1 year)
- **CSS:** Tailwind CDN

## Setup

```powershell
cd c:\Users\sarth\autoshare
.\venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

Fill `.env` (`SMTP_*`, `AUTOSHARE_SECRET_KEY`). See **Gmail App Password** below.

```powershell
.\venv\Scripts\python.exe -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

After model changes, delete `autoshare.db` once so tables recreate.

Replace `static/images/tip_qr.jpeg` with your own payment QR code for the About page tip button.

## Auth

- Register: `@iiitkottayam.ac.in` email + **username** + gender + phone → OTP
- Username must be a contiguous alphanumeric substring of the email local-part (e.g. `sarthak` from `sarthakpandey24bcy65@…`)
- Login: email → OTP; session lasts **1 year**
- Logout / Logout all devices
- **Async OTP:** login/register redirect to `/verify` immediately; emails send in a background FIFO queue (one at a time per Gmail account). The verify page polls `/api/otp-status` every 2s for send status.
- **Wrong flow redirects:** unregistered login → register (email prefilled); existing email on register → login (email prefilled)
- **Rate limits** (sliding window, in-memory per server):

| Endpoint | Per IP | Per email |
|----------|--------|-----------|
| Login / Register | 10 / 15 min | 5 / 15 min |
| Resend OTP | 10 / 15 min | 3 / 15 min (+ 60s cooldown) |
| Verify OTP | 30 / 15 min | — |

## Product rules

1. **24-hour rolling window** — book any departure from now up to now+24h
2. **Max 2 rides** — offer, join, or pending request count; any two must be **≥ 30 minutes** apart
3. Host must **approve** join requests (sees username, phone + gender)
4. Private pool contacts: phone + gender only
5. Public board does not show ride creator
6. Full / departed / cancelled rides leave the active board
7. **Ride history** (My rides only): departed + cancelled, last **7 days**
8. Dashboard **polls** `/api/dashboard` every 10s while the tab is visible

## Pages

| Path | Purpose |
|------|---------|
| `/`, `/about` | About + landing for guests; dashboard when logged in |
| `/api/dashboard`, `/api/otp-status` | JSON for polling |
| `/register`, `/login`, `/verify` | OTP auth |
| `/pool/create` | Offer a ride |

**Developed by Sarthak Pandey · Batch of 2024–2028**

## Gmail App Password

1. Enable 2-Step Verification on the sender Gmail
2. Create an [App Password](https://myaccount.google.com/apppasswords)
3. Set `SMTP_EMAIL` / `SMTP_APP_PASSWORD` in `.env` (never commit `.env`)

## Data model (high level)

- **students** — email, username, gender, phone
- **pools** — route, departure, fare, `cancelled_at`
- **pool_members** — membership with `left_at` for history
- **join_requests**, **otp_challenges** (`email_sent_at`, `email_send_failed`), **sessions**
