"""In-memory sliding-window rate limits for auth endpoints."""

from collections import defaultdict, deque
from datetime import datetime, timedelta
from threading import Lock

_lock = Lock()
_hits: dict[str, deque] = defaultdict(deque)

RATE_LIMIT_MESSAGE = "Too many attempts. Please wait a few minutes and try again."


def _client_ip(request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host or "unknown"
    return "unknown"


def _record(key: str, window: timedelta) -> int:
    """Record a hit and return count within the window."""
    now = datetime.now()
    cutoff = now - window
    with _lock:
        bucket = _hits[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        bucket.append(now)
        return len(bucket)


def check_rate_limit(request, *, email: str | None, ip_limit: int, ip_window_min: int, email_limit: int | None = None, email_window_min: int | None = None) -> str | None:
    """Return error message if limited, else None."""
    ip = _client_ip(request)
    ip_key = f"ip:{ip}"
    ip_count = _record(ip_key, timedelta(minutes=ip_window_min))
    if ip_count > ip_limit:
        return RATE_LIMIT_MESSAGE

    if email and email_limit and email_window_min:
        email_key = f"email:{email.lower()}"
        email_count = _record(email_key, timedelta(minutes=email_window_min))
        if email_count > email_limit:
            return RATE_LIMIT_MESSAGE
    return None
