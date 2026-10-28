from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.sql import func

from src.config.database import Base


class Organization(Base):
    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    organization_type = Column(String(100), default="General", nullable=False)
    status = Column(String(50), default="active", nullable=False)  # active, suspended

    # KYC Verification Fields
    official_email = Column(String(150), nullable=True)  # Official Mail ID
    contact_person = Column(String(150), nullable=True)  # Contact Person Name
    gst_number = Column(String(50), nullable=True)      # GST Number
    full_address = Column(String(500), nullable=True)    # Full Address
    tax_id = Column(String(50), nullable=True)  # GSTIN / Tax Identification Number
    pan_number = Column(String(50), nullable=True)  # PAN or Business Registration Number
    contact_phone = Column(String(50), nullable=True)  # Primary Organization Contact Phone
    address = Column(String(255), nullable=True)  # Business Street Address
    city = Column(String(100), nullable=True)
    state = Column(String(100), nullable=True)
    postal_code = Column(String(20), nullable=True)
    identity_proof_type = Column(String(100), nullable=True)  # GST Certificate, PAN Card, Incorporation Cert, Trade License
    identity_proof_number = Column(String(100), nullable=True)
    kyc_status = Column(String(50), default="verified", nullable=False)  # verified, pending, rejected

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
