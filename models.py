from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Table, UniqueConstraint
from sqlalchemy.orm import relationship
from database import Base

pool_members = Table(
    "pool_members",
    Base.metadata,
    Column("student_id", Integer, ForeignKey("students.id"), primary_key=True),
    Column("pool_id", Integer, ForeignKey("pools.id"), primary_key=True),
)


class Student(Base):
    __tablename__ = "students"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    gender = Column(String, nullable=False)  # "male" or "female"
    phone = Column(String, nullable=False)

    created_pools = relationship("Pool", back_populates="creator")
    joined_pools = relationship("Pool", secondary=pool_members, back_populates="members")
    join_requests = relationship("JoinRequest", back_populates="student", cascade="all, delete-orphan")
    sessions = relationship("UserSession", back_populates="student", cascade="all, delete-orphan")

    @property
    def gender_label(self) -> str:
        return "Male" if self.gender == "male" else "Female"


class Pool(Base):
    __tablename__ = "pools"

    id = Column(Integer, primary_key=True, index=True)
    creator_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    source = Column(String, nullable=False)
    destination = Column(String, nullable=False)
    departure_time = Column(DateTime, nullable=False)
    total_fare = Column(Integer, nullable=False)
    max_capacity = Column(Integer, default=4)

    creator = relationship("Student", back_populates="created_pools")
    members = relationship("Student", secondary=pool_members, back_populates="joined_pools")
    join_requests = relationship(
        "JoinRequest",
        back_populates="pool",
        cascade="all, delete-orphan",
        order_by="JoinRequest.created_at",
    )

    @property
    def remaining_seats(self):
        return max(0, self.max_capacity - (1 + len(self.members)))

    @property
    def headcount(self):
        return 1 + len(self.members)

    @property
    def fare_per_head(self):
        return self.total_fare // self.headcount

    @property
    def pending_requests(self):
        return [r for r in self.join_requests if r.status == "pending"]

    @property
    def participants(self):
        """Creator + approved members (for private contact list)."""
        return [self.creator] + list(self.members)


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
    phone = Column(String, nullable=True)  # signup only
    gender = Column(String, nullable=True)  # signup only
    expires_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class UserSession(Base):
    __tablename__ = "sessions"

    id = Column(String, primary_key=True)  # opaque token
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    expires_at = Column(DateTime, nullable=False)

    student = relationship("Student", back_populates="sessions")
