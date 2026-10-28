from typing import Optional
from pydantic import BaseModel, ConfigDict, model_validator

class TelephonySettingsBase(BaseModel):
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_phone_number: Optional[str] = None
    twilio_phone_number_sid: Optional[str] = None
    twilio_webhook_url: Optional[str] = None
    sip_domain: Optional[str] = None
    sip_credential_list_sid: Optional[str] = None
    sip_ip_access_control_list_sid: Optional[str] = None

class TelephonySettingsUpdate(TelephonySettingsBase):
    pass

class TelephonySettingsOut(TelephonySettingsBase):
    model_config = ConfigDict(from_attributes=True)

class TestConnectionRequest(BaseModel):
    account_sid: str
    auth_token: str

class TestConnectionResponse(BaseModel):
    success: bool
    message: str

class ValidateSipRequest(BaseModel):
    account_sid: str
    auth_token: str
    domain: str

class ValidateSipResponse(BaseModel):
    success: bool
    message: str


class SipConfigIn(BaseModel):
    account_name: Optional[str] = None
    sip_server: Optional[str] = None
    sip_proxy: Optional[str] = None
    sip_username: Optional[str] = None
    domain: Optional[str] = None
    login: Optional[str] = None
    sip_password: Optional[str] = None
    display_name: Optional[str] = None
    voicemail_number: Optional[str] = None
    dialing_prefix: Optional[str] = None
    dial_plan: Optional[str] = None
    hide_caller_id: Optional[str] = "Disabled"
    media_encryption: Optional[str] = "Disabled"
    transport: Optional[str] = "UDP"
    public_address: Optional[str] = "Auto"
    register_refresh: Optional[int] = 300
    keep_alive: Optional[int] = 15
    publish_presence: Optional[bool] = False
    allow_ip_rewrite: Optional[bool] = False
    ice: Optional[bool] = False
    disable_session_timers: Optional[bool] = False
    sip_host: Optional[str] = None
    sip_port: Optional[int] = 5060
    did_number: Optional[str] = None


class SipConfigOut(BaseModel):
    id: Optional[int] = None
    account_name: Optional[str] = None
    sip_server: Optional[str] = None
    sip_proxy: Optional[str] = None
    sip_username: Optional[str] = None
    domain: Optional[str] = None
    login: Optional[str] = None
    display_name: Optional[str] = None
    voicemail_number: Optional[str] = None
    dialing_prefix: Optional[str] = None
    dial_plan: Optional[str] = None
    hide_caller_id: Optional[str] = "Disabled"
    media_encryption: Optional[str] = "Disabled"
    transport: Optional[str] = "UDP"
    public_address: Optional[str] = "Auto"
    register_refresh: Optional[int] = 300
    keep_alive: Optional[int] = 15
    publish_presence: Optional[bool] = False
    allow_ip_rewrite: Optional[bool] = False
    ice: Optional[bool] = False
    disable_session_timers: Optional[bool] = False
    sip_host: Optional[str] = None
    sip_port: Optional[int] = 5060
    did_number: Optional[str] = None
    sip_extension: Optional[str] = "1001"
    sip_ws_url: Optional[str] = "wss://49.249.142.147:8089/ws"
    stun_server: Optional[str] = "stun:stun.l.google.com:19302"
    has_password: bool = False
    status: Optional[str] = "disconnected"
    registration_status: Optional[str] = "unregistered"
    registration_detail: Optional[str] = None
    last_connected_at: Optional[str] = None
    last_registered_at: Optional[str] = None
    total_calls: int = 0
    active_calls: int = 0
    last_call_at: Optional[str] = None
    latency_ms: Optional[int] = None
    connection_detail: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class SipTestRequest(BaseModel):
    sip_host: Optional[str] = None
    sip_server: Optional[str] = None
    sip_proxy: Optional[str] = None
    domain: Optional[str] = None
    sip_username: Optional[str] = None
    sip_password: Optional[str] = None
    sip_port: Optional[int] = 5060


class SipTestResponse(BaseModel):
    success: bool
    message: str
    status: str
    latency_ms: Optional[int] = None
    connection_detail: Optional[str] = None


class SipInboundPayload(BaseModel):
    caller_phone: str = "Unknown"
    caller_name: str = "SIP Caller"
    call_id: Optional[str] = None
    trunk_host: Optional[str] = None
    did_number: Optional[str] = None
    to_phone: Optional[str] = None
    sip_username: Optional[str] = None
    organization_id: Optional[int] = None
    # Webhook-mode providers may deliver pre-transcribed text
    transcription: Optional[str] = None
    event: Optional[str] = "call_start"  # call_start | transcription | call_end


class SipAudioChunkPayload(BaseModel):
    """Payload for streaming base64-encoded audio during an active call."""
    call_sid: str
    audio_base64: str
    encoding: Optional[str] = "pcm"   # pcm | ulaw


class SipCallEndPayload(BaseModel):
    """Payload to terminate a call."""
    call_sid: str
    reason: Optional[str] = "hangup"


class TestStepResult(BaseModel):
    step: str
    name: str
    passed: bool
    message: str
    latency_ms: Optional[int] = None
    detail: Optional[str] = None


class SipE2ETestRequest(BaseModel):
    sip_host: Optional[str] = None
    sip_username: Optional[str] = None
    sip_port: Optional[int] = 5060
    simulate_call: bool = True


class SipE2ETestResponse(BaseModel):
    overall_success: bool
    status: str
    message: str
    steps: list[TestStepResult]
    timestamp: str


# ─────────────────────────────────────────────────────────────
# SIP Registration schemas
# ─────────────────────────────────────────────────────────────

class SipRegisterRequest(BaseModel):
    """Trigger SIP REGISTER (uses saved config if fields omitted)."""
    account_name: Optional[str] = None
    sip_server: Optional[str] = None
    sip_proxy: Optional[str] = None
    domain: Optional[str] = None
    sip_username: Optional[str] = None
    login: Optional[str] = None
    sip_password: Optional[str] = None
    sip_host: Optional[str] = None
    sip_port: Optional[int] = 5060
    did_number: Optional[str] = None


class SipRegistrationStatusResponse(BaseModel):
    """Live registration status from the SIP UA."""
    status: str                          # registered | unregistered | registering | error
    detail: str
    sip_host: Optional[str] = None
    sip_username: Optional[str] = None
    sip_port: Optional[int] = 5060
    did_number: Optional[str] = None
    registered_at: Optional[str] = None
    last_attempt_at: Optional[str] = None
    error_message: Optional[str] = None
    active_calls: int = 0


class SipUnregisterResponse(BaseModel):
    success: bool
    message: str


class SipOutboundCallRequest(BaseModel):
    to_number: Optional[str] = None
    to_phone: Optional[str] = None
    from_number: Optional[str] = None
    caller_name: Optional[str] = None

    @model_validator(mode="before")
    def normalize_to_number(cls, values):
        if isinstance(values, dict):
            num = values.get("to_number") or values.get("to_phone") or values.get("phone")
            if num:
                values["to_number"] = str(num)
        return values


class SipOutboundCallResponse(BaseModel):
    success: bool
    call_sid: str
    to_number: str
    formatted_number: Optional[str] = None
    status: str
    message: str
