from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func
from src.config.database import Base


class Department(Base):
    __tablename__ = "departments"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, default=1)
    name = Column(String(100), nullable=False)
    code = Column(String(50), nullable=True)
    description = Column(Text, nullable=True)
    head_of_department = Column(String(150), nullable=True)
    phone = Column(String(50), nullable=True)
    email = Column(String(200), nullable=True)
    status = Column(String(20), default="Active", nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
