from sqlalchemy import Column, DateTime, Integer, String, Boolean
from sqlalchemy.sql import func

from src.config.database import Base


class SipConfig(Base):
    __tablename__ = "sip_configs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, default=1, index=True)
    organization_id = Column(Integer, default=1, index=True)

    # Reference Account Fields
    account_name = Column(String(100), nullable=True)
    sip_server = Column(String(255), nullable=True)
    sip_proxy = Column(String(255), nullable=True)
    sip_username = Column(String(100), nullable=True)
    domain = Column(String(255), nullable=True)
    login = Column(String(100), nullable=True)
    sip_password_encrypted = Column(String(500), nullable=True)

    display_name = Column(String(100), nullable=True)
    voicemail_number = Column(String(50), nullable=True)
    dialing_prefix = Column(String(50), nullable=True)
    dial_plan = Column(String(255), nullable=True)

    hide_caller_id = Column(String(20), default="Disabled")
    media_encryption = Column(String(20), default="Disabled")
    transport = Column(String(10), default="UDP")
    public_address = Column(String(20), default="Auto")

    register_refresh = Column(Integer, default=300)
    keep_alive = Column(Integer, default=15)

    publish_presence = Column(Boolean, default=False)
    allow_ip_rewrite = Column(Boolean, default=False)
    ice = Column(Boolean, default=False)
    disable_session_timers = Column(Boolean, default=False)

    # Legacy & Network fields
    sip_host = Column(String(255), nullable=True)
    sip_port = Column(Integer, default=5060)
    did_number = Column(String(50), nullable=True)        # Business DID / phone number
    sip_extension = Column(String(50), default="1001")
    sip_ws_url = Column(String(255), default="wss://49.249.142.147:8089/ws")
    stun_server = Column(String(255), default="stun:stun.l.google.com:19302")
    status = Column(String(50), default="disconnected")   # network probe: "connected" / "disconnected"
    registration_status = Column(String(50), default="unregistered")  # SIP UA: "registered" / "unregistered" / "registering" / "error"
    registration_detail = Column(String(500), nullable=True)          # last registration attempt detail
    last_connected_at = Column(DateTime(timezone=True), nullable=True)
    last_registered_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

