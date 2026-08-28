"""FIFO background worker for OTP emails (one at a time)."""

import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime

from database import SessionLocal
import models
from email_service import EmailSendError, send_otp_email

logger = logging.getLogger(__name__)

_job_queue: queue.Queue = queue.Queue()
_started = False
_start_lock = threading.Lock()


@dataclass
class OtpEmailJob:
    challenge_id: int
    email: str
    otp: str


def _worker_loop() -> None:
    while True:
        job = _job_queue.get()
        try:
            send_otp_email(job.email, job.otp)
            db = SessionLocal()
            try:
                challenge = db.query(models.OtpChallenge).filter_by(id=job.challenge_id).first()
                if challenge:
                    challenge.email_sent_at = datetime.now()
                    challenge.email_send_failed = False
                    db.commit()
            finally:
                db.close()
        except EmailSendError:
            logger.exception("OTP email failed for challenge %s", job.challenge_id)
            db = SessionLocal()
            try:
                challenge = db.query(models.OtpChallenge).filter_by(id=job.challenge_id).first()
                if challenge:
                    challenge.email_send_failed = True
                    db.commit()
            finally:
                db.close()
        except Exception:
            logger.exception("Unexpected OTP queue error for challenge %s", job.challenge_id)
            db = SessionLocal()
            try:
                challenge = db.query(models.OtpChallenge).filter_by(id=job.challenge_id).first()
                if challenge:
                    challenge.email_send_failed = True
                    db.commit()
            finally:
                db.close()
        finally:
            _job_queue.task_done()


def ensure_worker_started() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        thread = threading.Thread(target=_worker_loop, name="otp-email-worker", daemon=True)
        thread.start()
        _started = True


def enqueue_otp_email(challenge_id: int, email: str, otp: str) -> None:
    ensure_worker_started()
    _job_queue.put(OtpEmailJob(challenge_id=challenge_id, email=email, otp=otp))
