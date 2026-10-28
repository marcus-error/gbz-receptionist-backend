from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, HttpUrl, field_validator


class WebhookCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=255, description="Friendly name of the webhook")
    type: str = Field(default="outgoing", description="'outgoing' or 'incoming'")
    url: Optional[str] = Field(default=None, max_length=1024, description="Destination URL for outgoing webhooks")
    events: List[str] = Field(default_factory=list, description="Subscribed or accepted event names")
    is_active: bool = Field(default=True, description="Enable or disable webhook")
    description: Optional[str] = Field(default=None, max_length=500)

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        v_clean = v.strip().lower()
        if v_clean not in ("outgoing", "incoming"):
            raise ValueError("Type must be either 'outgoing' or 'incoming'")
        return v_clean

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: Optional[str], values) -> Optional[str]:
        if v:
            v = v.strip()
            if not (v.startswith("http://") or v.startswith("https://")):
                raise ValueError("Destination URL must start with http:// or https://")
        return v


class WebhookUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=255)
    url: Optional[str] = Field(default=None, max_length=1024)
    events: Optional[List[str]] = None
    is_active: Optional[bool] = None
    description: Optional[str] = None


class WebhookOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    webhook_uuid: Optional[str] = None
    organization_id: int
    name: str
    type: str
    url: Optional[str] = None
    secret_masked: str
    events: List[str]
    is_active: bool
    description: Optional[str] = None
    incoming_url: Optional[str] = None
    last_triggered_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class WebhookWithSecretOut(WebhookOut):
    secret: str  # Plaintext secret shown only on create/rotate


class WebhookDeliveryOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    webhook_id: int
    webhook_name: Optional[str] = None
    organization_id: int
    event: str
    event_id: str
    direction: str
    request_url: Optional[str] = None
    request_headers: Optional[Dict[str, Any]] = None
    request_payload: Optional[Dict[str, Any]] = None
    response_status: Optional[int] = None
    response_headers: Optional[Dict[str, Any]] = None
    response_body: Optional[str] = None
    attempt: int
    max_attempts: int
    status: str
    duration_ms: Optional[int] = None
    error_message: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class WebhookEventInfo(BaseModel):
    event: str
    category: str
    description: str
    sample_payload: Dict[str, Any]


class WebhookStatsOut(BaseModel):
    total_webhooks: int
    active_webhooks: int
    incoming_webhooks: int
    outgoing_webhooks: int
    failed_deliveries: int
    total_deliveries: int


class WebhookTestRequest(BaseModel):
    event: Optional[str] = None
    custom_data: Optional[Dict[str, Any]] = None


class WebhookTestResponse(BaseModel):
    success: bool
    status_code: Optional[int] = None
    duration_ms: Optional[int] = None
    request_headers: Optional[Dict[str, str]] = None
    request_payload: Optional[Dict[str, Any]] = None
    response_body: Optional[str] = None
    error_message: Optional[str] = None
