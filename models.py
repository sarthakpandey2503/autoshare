from datetime import datetime

from sqlalchemy import Boolean, Column, Integer, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from database import Base


class Student(Base):
    __tablename__ = "students"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    username = Column(String, unique=True, index=True, nullable=False)
    gender = Column(String, nullable=False)  # "male" or "female"
    phone = Column(String, nullable=False)

    created_pools = relationship("Pool", back_populates="creator")
    memberships = relationship("PoolMembership", back_populates="student", cascade="all, delete-orphan")
    join_requests = relationship("JoinRequest", back_populates="student", cascade="all, delete-orphan")
    sessions = relationship("UserSession", back_populates="student", cascade="all, delete-orphan")

    @property
    def gender_label(self) -> str:
        return "Male" if self.gender == "male" else "Female"


class PoolMembership(Base):
    __tablename__ = "pool_members"

    student_id = Column(Integer, ForeignKey("students.id"), primary_key=True)
    pool_id = Column(Integer, ForeignKey("pools.id"), primary_key=True)
    joined_at = Column(DateTime, nullable=False, default=datetime.now)
    left_at = Column(DateTime, nullable=True)

    student = relationship("Student", back_populates="memberships")
    pool = relationship("Pool", back_populates="memberships")


class Pool(Base):
    __tablename__ = "pools"

    id = Column(Integer, primary_key=True, index=True)
    creator_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    source = Column(String, nullable=False)
    destination = Column(String, nullable=False)
    departure_time = Column(DateTime, nullable=False)
    total_fare = Column(Integer, nullable=False)
    max_capacity = Column(Integer, default=4)
    cancelled_at = Column(DateTime, nullable=True)

    creator = relationship("Student", back_populates="created_pools")
    memberships = relationship(
        "PoolMembership",
        back_populates="pool",
        cascade="all, delete-orphan",
    )
    join_requests = relationship(
        "JoinRequest",
        back_populates="pool",
        cascade="all, delete-orphan",
        order_by="JoinRequest.created_at",
    )

    @property
    def active_memberships(self):
        return [m for m in self.memberships if m.left_at is None]

    @property
    def members(self):
        """Active (non-left) joiners only."""
        return [m.student for m in self.active_memberships]

    @property
    def remaining_seats(self):
        return max(0, self.max_capacity - (1 + len(self.active_memberships)))

    @property
    def headcount(self):
        return 1 + len(self.active_memberships)

    @property
    def fare_per_head(self):
        return self.total_fare // max(1, self.headcount)

    @property
    def pending_requests(self):
        return [r for r in self.join_requests if r.status == "pending"]

    @property
    def participants(self):
        """Creator + active approved members (private contact list)."""
        return [self.creator] + self.members

    @property
    def is_active(self):
        return self.cancelled_at is None and self.departure_time > datetime.now()

    @property
    def ended_at(self):
        if self.cancelled_at:
            return self.cancelled_at
        return self.departure_time

    @property
    def end_reason(self):
        if self.cancelled_at:
            return "Cancelled"
        if self.departure_time <= datetime.now():
            return "Departed"
        return "Active"


class JoinRequest(Base):
    __tablename__ = "join_requests"
    __table_args__ = (
        UniqueConstraint("pool_id", "student_id", name="uq_pool_student_request"),
    )

    id = Column(Integer, primary_key=True, index=True)
    pool_id = Column(Integer, ForeignKey("pools.id"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    status = Column(String, nullable=False, default="pending")
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    pool = relationship("Pool", back_populates="join_requests")
    student = relationship("Student", back_populates="join_requests")


class OtpChallenge(Base):
    __tablename__ = "otp_challenges"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, index=True, nullable=False)
    otp_hash = Column(String, nullable=False)
    purpose = Column(String, nullable=False)  # signup | login
    phone = Column(String, nullable=True)
    gender = Column(String, nullable=True)
    username = Column(String, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    email_sent_at = Column(DateTime, nullable=True)
    email_send_failed = Column(Boolean, nullable=False, default=False)


class UserSession(Base):
    __tablename__ = "sessions"

    id = Column(String, primary_key=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    expires_at = Column(DateTime, nullable=False)

    student = relationship("Student", back_populates="sessions")
