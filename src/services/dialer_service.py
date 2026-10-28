import logging
import threading
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session
from src.config.database import SessionLocal
from src.models.lead import Lead

logger = logging.getLogger(__name__)

class DialerService:
    def __init__(self, db: Session):
        self.db = db

    def process_due_leads(self) -> dict:
        """Process leads that are pending and their scheduled call time has arrived."""
        now = datetime.now(timezone.utc)
        
        # Get pending leads whose call_time is now or in the past
        due_leads = (
            self.db.query(Lead)
            .filter(
                Lead.status == "pending",
                Lead.call_time <= now
            )
            .order_by(Lead.call_time.asc())
            .limit(20) # Process in batches
            .all()
        )
        
        processed = 0
        failed = 0
        
        for lead in due_leads:
            # Mark as calling to prevent double processing
            lead.status = "calling"
            lead.updated_at = now
            self.db.commit()
            
            try:
                from src.services.telephony_service import TelephonyService
                telephony_svc = TelephonyService(self.db, org_id=lead.organization_id)
                
                # Mock a payload struct expected by make_outbound_call
                class CallPayload:
                    def __init__(self, to_phone):
                        self.to_phone = to_phone
                        self.from_number = None # Uses default if None

                payload = CallPayload(lead.phone)
                
                logger.info(f"[DialerService] Initiating call for Lead {lead.id} to {lead.phone}")
                call_info = telephony_svc.make_outbound_call(payload, lead_id=lead.id, outbound_script=lead.script)
                
                if getattr(call_info, 'get', None) and call_info.get("call_id"):
                     lead.call_log_id = call_info.get("call_id")
                     self.db.commit()

                processed += 1
            except Exception as exc:
                logger.error(f"[DialerService] Failed to call Lead {lead.id}: {exc}")
                lead.status = "failed"
                self.db.commit()
                failed += 1
                
        return {"processed": processed, "failed": failed}


_dialer_stop_event = threading.Event()
_dialer_thread = None

def _dialer_worker_loop():
    logger.info("[DialerWorker] Started background dialer loop...")
    while not _dialer_stop_event.is_set():
        try:
            db = SessionLocal()
            try:
                svc = DialerService(db)
                svc.process_due_leads()
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[DialerWorker] Crash in dialer loop: {e}")
        
        # Check for due leads every 15 seconds
        _dialer_stop_event.wait(15.0)

def start_dialer_worker():
    global _dialer_thread
    if _dialer_thread is None or not _dialer_thread.is_alive():
        _dialer_stop_event.clear()
        _dialer_thread = threading.Thread(target=_dialer_worker_loop, daemon=True, name="DialerWorkerThread")
        _dialer_thread.start()
        logger.info("[DialerWorker] Thread spawned.")

def stop_dialer_worker():
    if _dialer_thread and _dialer_thread.is_alive():
        logger.info("[DialerWorker] Stopping worker...")
        _dialer_stop_event.set()
        _dialer_thread.join(timeout=5.0)
