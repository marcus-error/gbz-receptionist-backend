"""
Central production configuration for the receptionist backend.

Place at:
    receptionist-backend/src/config/env.py

Secrets are loaded from environment variables / .env and are never returned
by API responses. OAuth access/refresh tokens should be encrypted before
persistent storage; this module exposes TOKEN_ENCRYPTION_KEY for that purpose.

Required/optional integrations:
- Twilio
- WhatsApp Cloud API
- Google OAuth
- Microsoft OAuth
- LLM
- STT
- TTS
- SMTP/email
- Database
- API authentication
- Webhook validation
- Background workers
- Production deployment
"""

import logging
import os
import secrets
from functools import lru_cache
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ------------------------------------------------------------------
    # Application / database
    # ------------------------------------------------------------------
    APP_ENV: str = Field(default="development")
    APP_NAME: str = Field(default="AI Receptionist")
    APP_HOST: str = Field(default="0.0.0.0")
    APP_PORT: int = Field(default=8000)
    PORT: Optional[int] = Field(default=None)  # Railway dynamic PORT support

    @field_validator("PORT", mode="before")
    @classmethod
    def empty_port_to_none(cls, value):
        if value == "" or value is None:
            return None
        return value
    API_PREFIX: str = Field(default="/api/v1")
    DATABASE_URL: str = Field(default="sqlite:///./receptionist.db")
    DB_AUTO_CREATE: bool = Field(default=True)
    RAG_DATABASE_PATH: str = Field(default="./rag.sqlite")
    CHROMA_DB_PATH: str = Field(default="./chroma_db")

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def empty_db_to_default(cls, value):
        if not value:
            return "sqlite:///./receptionist.db"
        return value

    @field_validator("CHROMA_DB_PATH", mode="before")
    @classmethod
    def empty_chroma_to_default(cls, value):
        if not value:
            return "./chroma_db"
        return value

    # API authentication
    API_AUTH_ENABLED: bool = Field(default=True)
    API_SECRET_KEY: str = Field(default="")
    JWT_SECRET_KEY: str = Field(default="")
    JWT_ALGORITHM: str = Field(default="HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=60)

    # Secure credential/token encryption
    TOKEN_ENCRYPTION_KEY: str = Field(default="")
    CREDENTIAL_ENCRYPTION_ENABLED: bool = Field(default=True)

    # ------------------------------------------------------------------
    # Twilio (Legacy - Preserved)
    # ------------------------------------------------------------------
    TWILIO_ACCOUNT_SID: Optional[str] = None
    TWILIO_AUTH_TOKEN: Optional[str] = None
    TWILIO_PHONE_NUMBER: Optional[str] = None
    TWILIO_PHONE_NUMBER_SID: Optional[str] = None
    TWILIO_API_KEY_SID: Optional[str] = None
    TWILIO_API_KEY_SECRET: Optional[str] = None
    TWILIO_WEBHOOK_URL: Optional[str] = None
    TWILIO_STATUS_CALLBACK_URL: Optional[str] = None
    TWILIO_RECORDING_CALLBACK_URL: Optional[str] = None

    # ------------------------------------------------------------------
    # SIP Telephony Configuration
    # ------------------------------------------------------------------
    SIP_HOST: Optional[str] = None
    SIP_ACCOUNT_NAME: Optional[str] = None
    SIP_SERVER: Optional[str] = None
    SIP_PROXY: Optional[str] = None
    SIP_USERNAME: Optional[str] = None
    SIP_DOMAIN: Optional[str] = None
    SIP_LOGIN: Optional[str] = None
    SIP_PASSWORD: Optional[str] = None
    SIP_DISPLAY_NAME: Optional[str] = None
    SIP_VOICEMAIL_NUMBER: Optional[str] = None
    SIP_DIALING_PREFIX: Optional[str] = None
    SIP_DIAL_PLAN: Optional[str] = None
    SIP_HIDE_CALLER_ID: str = "Disabled"
    SIP_MEDIA_ENCRYPTION: str = "Disabled"
    SIP_TRANSPORT: str = "UDP"
    SIP_PUBLIC_ADDRESS: str = "Auto"
    SIP_REGISTER_REFRESH: int = 300
    SIP_KEEP_ALIVE: int = 15
    SIP_PUBLISH_PRESENCE: bool = False
    SIP_ALLOW_IP_REWRITE: bool = False
    SIP_ICE: bool = False
    SIP_DISABLE_SESSION_TIMERS: bool = False
    SIP_PORT: int = 5060
    SIP_DID_NUMBER: Optional[str] = None
    SIP_WEBHOOK_BASE_URL: Optional[str] = None
    SIP_EXTENSION: Optional[str] = Field(default="1001")
    SIP_WS_URL: Optional[str] = Field(default="wss://49.249.142.147:8089/ws")
    STUN_SERVER: Optional[str] = Field(default="stun:stun.l.google.com:19302")
    CALL_INACTIVITY_TIMEOUT_SECONDS: int = Field(default=600)

    # ------------------------------------------------------------------
    # WhatsApp Cloud API
    # ------------------------------------------------------------------
    WHATSAPP_ACCESS_TOKEN: Optional[str] = None
    WHATSAPP_PHONE_NUMBER_ID: Optional[str] = None
    WHATSAPP_WABA_ID: Optional[str] = None
    WHATSAPP_VERIFY_TOKEN: Optional[str] = None
    WHATSAPP_APP_SECRET: Optional[str] = None
    WHATSAPP_GRAPH_VERSION: str = "v23.0"
    WHATSAPP_LANGUAGE: str = "en_US"
    WHATSAPP_REMINDER_TEMPLATE: str = "appointment_reminder"

    # ------------------------------------------------------------------
    # Google OAuth
    # ------------------------------------------------------------------
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None
    GOOGLE_REDIRECT_URI: Optional[str] = None
    GOOGLE_SCOPES: str = (
        "https://www.googleapis.com/auth/calendar "
        "https://www.googleapis.com/auth/userinfo.email"
    )

    # ------------------------------------------------------------------
    # Microsoft OAuth
    # ------------------------------------------------------------------
    MICROSOFT_CLIENT_ID: Optional[str] = None
    MICROSOFT_CLIENT_SECRET: Optional[str] = None
    MICROSOFT_REDIRECT_URI: Optional[str] = None
    MICROSOFT_TENANT_ID: str = "common"

    # ------------------------------------------------------------------
    # LLM (Gemini / Mistral / xAI Grok / Ollama)
    # ------------------------------------------------------------------
    LLM_PROVIDER: str = "gemini"  # "gemini", "mistral", "xai", "grok", "ollama", "openai"
    LLM_MODEL: str = "gemini-3.5-flash-lite"
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_MODEL: str = "gemini-3.5-flash-lite"
    MISTRAL_API_KEY: Optional[str] = None
    MISTRAL_MODEL: str = "mistral-tiny"
    MISTRAL_BASE_URL: str = "https://api.mistral.ai/v1"
    XAI_API_KEY: Optional[str] = None
    XAI_MODEL: str = "grok-4.6"
    XAI_BASE_URL: str = "https://api.x.ai/v1"
    OLLAMA_BASE_URL: str = "http://localhost:11434/v1"
    OLLAMA_MODEL: str = "llama3.2"
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = "gpt-4o-mini"
    ANTHROPIC_API_KEY: Optional[str] = None
    ANTHROPIC_MODEL: str = "claude-3-5-sonnet-latest"

    # ------------------------------------------------------------------
    # RAG & Embeddings Configuration
    # ------------------------------------------------------------------
    EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
    TOP_K: int = 5
    SIMILARITY_THRESHOLD: float = 0.35
    ORGANIZATION_ID: str = "default_org"

    # ------------------------------------------------------------------
    # STT (Deepgram Streaming / Open-Source Fallback)
    # ------------------------------------------------------------------
    STT_PROVIDER: str = "deepgram"  # "deepgram", "faster-whisper", "whisper", "local"
    DEEPGRAM_API_KEY: Optional[str] = None
    DEEPGRAM_MODEL: str = "nova-2-phonecall"
    DEEPGRAM_ENDPOINTING_MS: int = 300
    DEEPGRAM_UTTERANCE_END_MS: int = 1000
    STT_MODEL_SIZE: str = "tiny"
    STT_API_KEY: Optional[str] = None
    WHISPER_MODEL: str = "tiny"
    CUSTOMER_SILENCE_END_MS: int = Field(default=400)
    # Maximum idle time (seconds) before a live call is gracefully ended.
    # Default: 600s (10 minutes). Configurable via CALL_INACTIVITY_TIMEOUT_SECONDS in .env
    CALL_INACTIVITY_TIMEOUT_SECONDS: int = Field(default=600)


    # ------------------------------------------------------------------
    # NLP / Intent Detection (Local Open-Source)
    # ------------------------------------------------------------------
    NLP_MODEL: str = "local"

    # ------------------------------------------------------------------
    # Emotion Detection (External API only)
    # ------------------------------------------------------------------
    EMOTION_API_KEY: Optional[str] = None
    EMOTION_API_URL: Optional[str] = None
    EMOTION_API_TIMEOUT: float = 1.5

    # ------------------------------------------------------------------
    # TTS Configuration (Murf AI / ElevenLabs / Local)
    # ------------------------------------------------------------------
    TTS_PROVIDER: str = "murf"  # "murf", "elevenlabs", "local", "gtts"
    TTS_MODEL: str = "default"
    MURF_API_KEY: Optional[str] = "ap2_7a4fd792-2797-43cf-8815-bf7f5c1c009a"
    MURF_VOICE_ID: str = "en-US-natalie"
    MURF_MODEL: str = "GEN2"
    MURF_API_URL: str = "https://api.murf.ai/v1/speech/generate"
    ELEVENLABS_API_KEY: Optional[str] = None
    ELEVENLABS_VOICE_ID: str = "p9aflnsbBe1o0aDeQa97"
    ELEVENLABS_MODEL_ID: str = "eleven_multilingual_v2"
    TTS_SPEECH_SPEED: float = 1.0

    # ------------------------------------------------------------------
    # Email / SMTP
    # ------------------------------------------------------------------
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USERNAME: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_FROM_EMAIL: Optional[str] = None
    SMTP_FROM_NAME: str = "AI Receptionist"
    SMTP_ENCRYPTION: str = "TLS"
    EMAIL_REMINDER_TEMPLATE: str = "appointment_reminder"

    # ------------------------------------------------------------------
    # Webhook security
    # ------------------------------------------------------------------
    WEBHOOK_VALIDATION_ENABLED: bool = True
    WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS: int = 300
    TWILIO_VALIDATE_SIGNATURE: bool = True

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = (
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    LOG_FILE: Optional[str] = None

    # ------------------------------------------------------------------
    # Background workers / reminders
    # ------------------------------------------------------------------
    REMINDER_WORKER_ENABLED: bool = True
    REMINDER_WORKER_INTERVAL_SECONDS: int = 60
    REMINDER_MAX_ATTEMPTS: int = 3

    # ------------------------------------------------------------------
    # Production deployment
    # ------------------------------------------------------------------
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:5174,http://localhost:3000,http://127.0.0.1:5173,http://127.0.0.1:5174,http://127.0.0.1:3000"
    TRUSTED_HOSTS: str = "localhost,127.0.0.1"
    FORWARDED_ALLOW_IPS: str = "*"
    HTTPS_REQUIRED: bool = False
    TIMEZONE: str = Field(default="Asia/Kolkata")
    BUSINESS_TIMEZONE: str = Field(default="Asia/Kolkata")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("APP_ENV")
    @classmethod
    def validate_environment(cls, value):
        value = value.lower().strip()
        allowed = {"development", "test", "staging", "production"}
        if value not in allowed:
            raise ValueError(f"APP_ENV must be one of {sorted(allowed)}")
        return value

    @field_validator("TTS_SPEECH_SPEED")
    @classmethod
    def validate_speed(cls, value):
        return max(0.5, min(2.0, value))

    @property
    def cors_origins(self) -> list[str]:
        return [x.strip() for x in self.CORS_ORIGINS.split(",") if x.strip()]

    @property
    def trusted_hosts(self) -> list[str]:
        return [x.strip() for x in self.TRUSTED_HOSTS.split(",") if x.strip()]

    def validate_production_security(self):
        """
        Fail fast on unsafe production defaults.

        Development may use local defaults, but production must explicitly
        provide authentication/encryption/webhook secrets.
        """
        if self.APP_ENV != "production":
            return

        missing = []
        if self.API_AUTH_ENABLED and not self.API_SECRET_KEY:
            missing.append("API_SECRET_KEY")
        if self.API_AUTH_ENABLED and not self.JWT_SECRET_KEY:
            missing.append("JWT_SECRET_KEY")
        if self.CREDENTIAL_ENCRYPTION_ENABLED and not self.TOKEN_ENCRYPTION_KEY:
            missing.append("TOKEN_ENCRYPTION_KEY")

        if self.WEBHOOK_VALIDATION_ENABLED:
            if not self.WHATSAPP_VERIFY_TOKEN:
                missing.append("WHATSAPP_VERIFY_TOKEN")
            if not self.TWILIO_AUTH_TOKEN and self.TWILIO_VALIDATE_SIGNATURE:
                missing.append("TWILIO_AUTH_TOKEN")

        if missing:
            raise RuntimeError(
                "Missing required production security settings: "
                + ", ".join(missing)
            )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_production_security()
    return settings


settings = get_settings()


def configure_logging():
    """Configure application-wide structured-enough logging."""
    handlers = [logging.StreamHandler()]

    if settings.LOG_FILE:
        handlers.append(logging.FileHandler(settings.LOG_FILE, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format=settings.LOG_FORMAT,
        handlers=handlers,
        force=True,
    )


def generate_secret(length: int = 48) -> str:
    """Generate a cryptographically secure secret for deployment setup."""
    return secrets.token_urlsafe(length)


def production_uvicorn_command() -> str:
    """
    Recommended production process command.

    For multiple processes, run a process manager/container orchestration layer
    and keep the reminder worker enabled in only one dedicated worker process
    to avoid duplicate reminder delivery.
    """
    port = settings.PORT or settings.APP_PORT
    return (
        f"uvicorn main:app --host {settings.APP_HOST} "
        f"--port {port} --workers {settings.WORKERS}"
    )


def migration_commands() -> dict:
    """
    Standard Alembic migration lifecycle.

    The application should use migrations in production rather than relying on
    create_all() for schema changes.
    """
    return {
        "create": "alembic revision --autogenerate -m \"schema update\"",
        "upgrade": "alembic upgrade head",
        "downgrade": "alembic downgrade -1",
    }
