from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Table
from sqlalchemy.orm import relationship
from database import Base

# Association table for many-to-many relationship between Students and Pools
pool_members = Table(
    "pool_members",
    Base.metadata,
    Column("student_id", Integer, ForeignKey("students.id"), primary_key=True),
    Column("pool_id", Integer, ForeignKey("pools.id"), primary_key=True)
)

class Student(Base):
    __tablename__ = "students"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    phone = Column(String, nullable=False)

    # Relationships
    created_pools = relationship("Pool", back_populates="creator")
    joined_pools = relationship("Pool", secondary=pool_members, back_populates="members")

class Pool(Base):
    __tablename__ = "pools"

    id = Column(Integer, primary_key=True, index=True)
    creator_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    source = Column(String, nullable=False)
    destination = Column(String, nullable=False)
    departure_time = Column(DateTime, nullable=False)
    total_fare = Column(Integer, nullable=False)
    max_capacity = Column(Integer, default=4)

    # Relationships
    creator = relationship("Student", back_populates="created_pools")
    members = relationship("Student", secondary=pool_members, back_populates="joined_pools")

    @property
    def remaining_seats(self):
        # 1 slot taken by the creator, plus the count of additional joined members
        return self.max_capacity - (1 + len(self.members))