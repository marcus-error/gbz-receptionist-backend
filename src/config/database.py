"""
SQLAlchemy engine/session setup for MySQL.

No Prisma, no seed files - tables are created automatically on startup
(see `init_db()`, called from src/app.py) when DB_AUTO_CREATE=true.
Suitable for MySQL/PostgreSQL/SQLite; only the DB_* env vars need to change
to switch databases.
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from src.config.env import settings

connect_args = {}
if "sqlite" in settings.DATABASE_URL:
    connect_args = {"check_same_thread": False, "timeout": 60}
    engine = create_engine(settings.DATABASE_URL, connect_args=connect_args, echo=False)

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.execute("PRAGMA cache_size=-64000")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.close()
else:
    engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True, echo=False)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency that yields a DB session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables that don't exist yet and seed demo data on first boot."""
    # Import models here so they're registered on Base.metadata before create_all.
    from src.models import appointment, call_log, knowledge_doc, staff, user  # noqa: F401
    from src.models import organization, permission, subscription, department  # noqa: F401
    from src.models import settings as settings_model  # noqa: F401
    from src.models import twilio_config, sip_config  # noqa: F401
    from src.models import email_config, whatsapp_config, calendar_config, gemini_account  # noqa: F401
    from src.models import workflow  # noqa: F401
    from src.models import lead  # noqa: F401
    from src.models import webhook  # noqa: F401



    if settings.DB_AUTO_CREATE:
        Base.metadata.create_all(bind=engine)

    # Automatic schema sync/migration for existing tables
    try:
        from sqlalchemy import inspect, text
        inspector = inspect(engine)
        existing_tables = inspector.get_table_names()
        with engine.begin() as conn:
            for table_name, table in Base.metadata.tables.items():
                if table_name in existing_tables:
                    existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
                    for col in table.columns:
                        if col.name not in existing_cols:
                            col_type = col.type.compile(engine.dialect)
                            default_str = ""
                            if col.default is not None and col.default.arg is not None:
                                if isinstance(col.default.arg, (str, int, float, bool)):
                                    val = col.default.arg
                                    if isinstance(val, bool):
                                        val = 1 if val else 0
                                    elif isinstance(val, str):
                                        val = f"'{val}'"
                                    default_str = f" DEFAULT {val}"
                            sql = f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type}{default_str}"
                            conn.execute(text(sql))

            # Ensure MySQL TEXT columns that store large documents/scripts are upgraded to LONGTEXT
            if engine.dialect.name == "mysql":
                try:
                    conn.execute(text("ALTER TABLE knowledge_documents MODIFY COLUMN content LONGTEXT"))
                except Exception as ex:
                    print(f"[INFO] knowledge_documents.content LONGTEXT upgrade: {ex}")
                try:
                    conn.execute(text("ALTER TABLE call_logs MODIFY COLUMN outbound_script LONGTEXT"))
                    conn.execute(text("ALTER TABLE call_logs MODIFY COLUMN messages LONGTEXT"))
                    conn.execute(text("ALTER TABLE call_logs MODIFY COLUMN caller_said LONGTEXT"))
                    conn.execute(text("ALTER TABLE call_logs MODIFY COLUMN ai_response LONGTEXT"))
                except Exception:
                    pass
    except Exception as exc:
        print(f"[WARNING] database auto-migration warning: {exc}")

    # Backfill missing customer_id for appointments
    try:
        from src.models.appointment import Appointment
        import random
        db_session = SessionLocal()
        try:
            appts = db_session.query(Appointment).filter(
                (Appointment.customer_id == None) | (Appointment.customer_id == "")
            ).all()
            if appts:
                phone_map = {}
                for appt in appts:
                    phone_key = (appt.phone or "").strip()
                    if phone_key and phone_key in phone_map:
                        appt.customer_id = phone_map[phone_key]
                    else:
                        existing = None
                        if phone_key:
                            existing = db_session.query(Appointment).filter(
                                Appointment.phone == phone_key,
                                Appointment.customer_id != None,
                                Appointment.customer_id != ""
                            ).first()
                        if existing and existing.customer_id:
                            cid = existing.customer_id
                        else:
                            cid = f"CUST-{random.randint(10000, 99999)}"
                        appt.customer_id = cid
                        if phone_key:
                            phone_map[phone_key] = cid
                db_session.commit()
        finally:
            db_session.close()
    except Exception as exc:
        print(f"[WARNING] customer_id backfill warning: {exc}")

    # Synchronize historical calls and appointments into Leads table
    try:
        from src.services.lead_service import LeadService
        db_session = SessionLocal()
        try:
            LeadService(db_session).backfill_historical_data()
        finally:
            db_session.close()
    except Exception as exc:
        print(f"[WARNING] leads backfill warning: {exc}")

    # Seed demo data if the database is empty (safe no-op on subsequent boots).
    # Wrapped in try/except so a transient DB error never prevents the server from starting.
    try:
        from src.config.seed import seed_if_empty
        db = SessionLocal()
        try:
            seed_if_empty(db)
        finally:
            db.close()
    except Exception as exc:  # pragma: no cover
        print(f"[WARNING] Seed skipped due to error: {exc}")
