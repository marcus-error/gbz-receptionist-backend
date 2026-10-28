import os
from dotenv import load_dotenv
load_dotenv()

os.environ["ANONYMIZED_TELEMETRY"] = "False"

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config.database import init_db
from src.config.env import settings
from src.middleware.error_middleware import register_error_handlers
from src.middleware.logger_middleware import LoggerMiddleware
from src.routes.index import api_router
from src.services.stt_service import stt_service
from src.services.nlp_service import nlp_service
from src.services.tts_service import tts_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database and local open-source AI models...")
    init_db()
    
    # Pre-initialize local models gracefully in background
    try:
        stt_service.initialize_model()
        nlp_service.initialize_model()
        tts_service.initialize_model()
        from src.services.chroma_vector_store import chroma_vector_store
        _ = chroma_vector_store.embedding_engine._get_model()
    except Exception as exc:
        logger.warning(f"Background model initialization warning: {exc}. Server continuing startup.")

    # Pre-initialize reminder worker and SQLAlchemy hook
    try:
        from src.services.reminder_service import initialize_reminder_system
        initialize_reminder_system()
        logger.info("Reminder system initialized successfully.")
    except Exception as exc:
        logger.warning(f"Reminder system initialization warning: {exc}")

    # Start dialer worker
    try:
        from src.services.dialer_service import start_dialer_worker
        start_dialer_worker()
        logger.info("Dialer worker initialized successfully.")
    except Exception as exc:
        logger.warning(f"Dialer worker initialization warning: {exc}")


    # Auto-start SIP registrar(s) if credentials exist in DB or .env
    try:
        from src.config.database import SessionLocal
        from src.models.sip_config import SipConfig
        from src.services.sip_registrar import start_registrar
        from src.utils.security import decrypt_secret
        import threading

        _db = SessionLocal()
        try:
            # Query all registered configs or the primary config
            active_configs = _db.query(SipConfig).filter(SipConfig.registration_status.in_(["registered", "connected"])).all()
            if not active_configs:
                first_cfg = _db.query(SipConfig).first()
                if first_cfg:
                    active_configs = [first_cfg]

            if active_configs:
                for cfg in active_configs:
                    org_id = cfg.organization_id or 1
                    sip_host = (cfg.sip_host or cfg.domain or cfg.sip_server or (settings.SIP_HOST if org_id == 1 else None)) or ""
                    sip_username = (cfg.sip_username or cfg.login or (settings.SIP_USERNAME if org_id == 1 else None)) or ""
                    enc_pass = cfg.sip_password_encrypted or (settings.SIP_PASSWORD if org_id == 1 else None) or ""
                    sip_password = decrypt_secret(enc_pass) if enc_pass else ""
                    sip_port = cfg.sip_port or (settings.SIP_PORT if org_id == 1 else 5060) or 5060
                    did_number = cfg.did_number or (settings.SIP_DID_NUMBER if org_id == 1 else None) or sip_username

                    if sip_host and sip_username and sip_password:
                        logger.info(f"[SIP] Auto-starting SIP registrar for Org {org_id}: {sip_username}@{sip_host}:{sip_port}")
                        threading.Thread(
                            target=start_registrar,
                            kwargs={
                                "sip_host": sip_host,
                                "sip_username": sip_username,
                                "sip_password": sip_password,
                                "sip_port": int(sip_port),
                                "did_number": did_number,
                                "organization_id": org_id,
                            },
                            daemon=True,
                            name=f"sip-autostart-org{org_id}",
                        ).start()
            elif settings.SIP_HOST and settings.SIP_USERNAME and settings.SIP_PASSWORD:
                logger.info(f"[SIP] Auto-starting default admin SIP registrar from environment: {settings.SIP_USERNAME}@{settings.SIP_HOST}:{settings.SIP_PORT}")
                threading.Thread(
                    target=start_registrar,
                    kwargs={
                        "sip_host": settings.SIP_HOST,
                        "sip_username": settings.SIP_USERNAME,
                        "sip_password": settings.SIP_PASSWORD,
                        "sip_port": int(settings.SIP_PORT or 5060),
                        "did_number": getattr(settings, "SIP_DID_NUMBER", settings.SIP_USERNAME),
                        "organization_id": 1,
                    },
                    daemon=True,
                    name="sip-autostart-org1",
                ).start()
            else:
                logger.info("[SIP] No complete SIP credentials found in DB or .env — registrar not started.")
        finally:
            _db.close()
    except Exception as exc:
        logger.warning(f"[SIP] Auto-start registrar warning: {exc}")

    yield
    logger.info("Shutting down AI Receptionist backend...")

    # Safeguard active SIP calls against mid-call reload interruptions
    try:
        from src.services.sip_call_handler import active_call_count
        if active_call_count() > 0:
            logger.warning(f"[SIP SHUTDOWN GUARD] {active_call_count()} active SIP call(s) in progress. Waiting for call completion...")
            import time
            wait_limit = 2.0  # Fast reload grace period
            t_wait_start = time.time()
            while active_call_count() > 0 and (time.time() - t_wait_start) < wait_limit:
                time.sleep(0.2)
            logger.info(f"[SIP SHUTDOWN GUARD] Active calls concluded or grace period reached ({active_call_count()} calls remaining).")
    except Exception as exc:
        logger.warning(f"[SIP SHUTDOWN GUARD] {exc}")

    # Gracefully stop SIP registrar
    try:
        from src.services.sip_registrar import stop_registrar
        stop_registrar()
        logger.info("[SIP] Registrar stopped on shutdown.")
    except Exception as exc:
        logger.warning(f"[SIP] Registrar shutdown warning: {exc}")



def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version="1.0.0",
        lifespan=lifespan
    )

    # Build CORS origins list — always include common local dev ports and production domains
    cors_str = str(getattr(settings, "CORS_ORIGINS", "") or "").strip()
    dev_origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:5175",
        "http://127.0.0.1:5175",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://www.callzenza.in",
        "https://callzenza.in",
        "https://vr.aminoid.in",
    ]
    if cors_str:
        extra = [o.strip() for o in cors_str.split(",") if o.strip() and o.strip() != "*"]
        origins = list(dict.fromkeys(dev_origins + extra))  # deduplicated
    else:
        origins = dev_origins

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=r"^https?://.*$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )
    app.add_middleware(LoggerMiddleware)

    register_error_handlers(app)

    app.include_router(api_router, prefix="/api")
    app.include_router(api_router, prefix="/api/v1")
    app.include_router(api_router)

    # Serve uploaded audio recordings
    from fastapi.staticfiles import StaticFiles
    uploads_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "uploads"))
    os.makedirs(os.path.join(uploads_dir, "recordings"), exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")

    # Root WebSocket routes for direct WSS/WS connections
    from src.sockets.voice_socket import router as voice_router
    from src.sockets.twilio_media_stream import router as twilio_stream_router
    app.include_router(voice_router)
    app.include_router(twilio_stream_router)

    @app.get("/health", tags=["Health"])
    def health_check():
        """Health check endpoint required for Railway healthchecks."""
        return {"status": "ok"}

    from src.sockets.socket_manager import sio
    import socketio
    
    asgi_app = socketio.ASGIApp(sio, app)
    asgi_app.fastapi_app = app
    return asgi_app


app = create_app()
fastapi_app = app.fastapi_app

