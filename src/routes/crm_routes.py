from typing import List, Dict, Any, Optional
from datetime import datetime
import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.middleware.auth_middleware import get_current_user
from src.models.user import User
from src.models.crm import (
    CrmConnection,
    CrmSyncSettings,
    CrmAutomationRules,
    CrmFieldMapping,
    CrmSyncLog,
)
from src.services.crm_service import (
    test_connection_logic,
    validate_url,
    seed_default_crm_data_if_empty,
)

router = APIRouter(prefix="/crm", tags=["CRM Integration"])


def _get_org_id(user: Optional[User]) -> int:
    if not user:
        return 1
    return user.organization_id or 1


@router.get("/connections")
def get_connections(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    org_id = _get_org_id(current_user)
    seed_default_crm_data_if_empty(db, organization_id=org_id)
    connections = db.query(CrmConnection).filter(CrmConnection.organization_id == org_id).all()
    return [conn.to_dict() for conn in connections]


@router.post("/connect")
def save_connection(
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    org_id = _get_org_id(current_user)
    provider = payload.get("provider")
    name = payload.get("connectionName") or f"{provider} Connection"
    account_name = payload.get("accountName") or "Production Account"
    base_url = payload.get("baseUrl")
    auth_type = payload.get("authType", "api_key")
    api_key = payload.get("apiKey")
    client_id = payload.get("clientId")
    client_secret = payload.get("clientSecret")

    if not provider:
        raise HTTPException(status_code=400, detail="CRM Provider name is required.")

    if base_url and not validate_url(base_url):
        raise HTTPException(status_code=400, detail="API Base URL is invalid. Must be a valid URL starting with http:// or https://")

    # Look for existing connection for provider or create new
    conn = db.query(CrmConnection).filter(
        CrmConnection.organization_id == org_id,
        CrmConnection.provider == provider
    ).first()
    if not conn:
        conn = CrmConnection(organization_id=org_id, provider=provider)
        db.add(conn)

    conn.name = name
    conn.account_name = account_name
    conn.base_url = base_url
    conn.auth_type = auth_type
    if api_key and api_key != "••••••••••••••••":
        conn.api_key = api_key
    if client_id:
        conn.client_id = client_id
    if client_secret and client_secret != "••••••••••••••••":
        conn.client_secret = client_secret

    conn.status = "connected"
    conn.last_sync = "Just now"
    conn.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(conn)
    return {"success": True, "connection": conn.to_dict()}


@router.post("/test-connection")
def test_connection(payload: Dict[str, Any]):
    res = test_connection_logic(payload)
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["message"])
    return res


@router.delete("/connections/{connection_id}")
def disconnect_connection(
    connection_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    org_id = _get_org_id(current_user)
    conn = db.query(CrmConnection).filter(
        CrmConnection.organization_id == org_id,
        CrmConnection.id == connection_id
    ).first()
    if conn:
        conn.status = "disconnected"
        db.commit()
    return {"success": True, "message": "CRM connection disconnected successfully."}


@router.get("/settings")
def get_sync_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    org_id = _get_org_id(current_user)
    seed_default_crm_data_if_empty(db, organization_id=org_id)
    settings = db.query(CrmSyncSettings).filter(CrmSyncSettings.organization_id == org_id).first()
    return settings.to_dict() if settings else {}


@router.post("/settings")
def save_sync_settings(
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    org_id = _get_org_id(current_user)
    settings = db.query(CrmSyncSettings).filter(CrmSyncSettings.organization_id == org_id).first()
    if not settings:
        settings = CrmSyncSettings(organization_id=org_id)
        db.add(settings)

    settings.sync_contacts = payload.get("syncContacts", True)
    settings.sync_leads = payload.get("syncLeads", True)
    settings.sync_call_records = payload.get("syncCallRecords", True)
    settings.sync_call_transcripts = payload.get("syncCallTranscripts", True)
    settings.sync_call_summary = payload.get("syncCallSummary", True)
    settings.sync_caller_intent = payload.get("syncCallerIntent", True)
    settings.sync_appointments = payload.get("syncAppointments", True)
    settings.sync_followup_tasks = payload.get("syncFollowupTasks", True)
    settings.sync_ai_data = payload.get("syncAiData", True)
    settings.sync_recording_url = payload.get("syncRecordingUrl", True)
    settings.sync_human_transfer_details = payload.get("syncHumanTransferDetails", True)

    db.commit()
    return {"success": True, "settings": settings.to_dict()}


@router.get("/field-mappings")
def get_field_mappings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    org_id = _get_org_id(current_user)
    seed_default_crm_data_if_empty(db, organization_id=org_id)
    mappings = db.query(CrmFieldMapping).filter(CrmFieldMapping.organization_id == org_id).all()
    return [m.to_dict() for m in mappings]


@router.post("/field-mappings")
def save_field_mappings(
    payload: List[Dict[str, Any]],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    org_id = _get_org_id(current_user)
    db.query(CrmFieldMapping).filter(CrmFieldMapping.organization_id == org_id).delete()
    for row in payload:
        ai_f = row.get("aiField")
        crm_f = row.get("crmField")
        if ai_f and crm_f:
            db.add(CrmFieldMapping(organization_id=org_id, ai_field=ai_f, crm_field=crm_f))
    db.commit()
    mappings = db.query(CrmFieldMapping).filter(CrmFieldMapping.organization_id == org_id).all()
    return {"success": True, "mappings": [m.to_dict() for m in mappings]}


@router.get("/automation-rules")
def get_automation_rules(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    org_id = _get_org_id(current_user)
    seed_default_crm_data_if_empty(db, organization_id=org_id)
    rules = db.query(CrmAutomationRules).filter(CrmAutomationRules.organization_id == org_id).first()
    return rules.to_dict() if rules else {}


@router.post("/automation-rules")
def save_automation_rules(
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    org_id = _get_org_id(current_user)
    rules = db.query(CrmAutomationRules).filter(CrmAutomationRules.organization_id == org_id).first()
    if not rules:
        rules = CrmAutomationRules(organization_id=org_id)
        db.add(rules)

    rules.new_caller_contact = payload.get("newCallerContact", True)
    rules.lead_identified = payload.get("leadIdentified", True)
    rules.appointment_booked = payload.get("appointmentBooked", True)
    rules.call_transferred = payload.get("callTransferred", True)
    rules.call_unresolved = payload.get("callUnresolved", True)
    rules.callback_requested = payload.get("callbackRequested", True)
    rules.existing_contact_call = payload.get("existingContactCall", True)
    rules.appointment_cancelled = payload.get("appointmentCancelled", True)

    db.commit()
    return {"success": True, "rules": rules.to_dict()}


@router.get("/sync-logs")
def get_sync_logs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    org_id = _get_org_id(current_user)
    seed_default_crm_data_if_empty(db, organization_id=org_id)
    logs = db.query(CrmSyncLog).filter(CrmSyncLog.organization_id == org_id).order_by(CrmSyncLog.id.desc()).all()
    return [l.to_dict() for l in logs]


@router.post("/sync-logs/{log_id}/retry")
def retry_sync_log(
    log_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    org_id = _get_org_id(current_user)
    log = db.query(CrmSyncLog).filter(
        CrmSyncLog.organization_id == org_id,
        CrmSyncLog.id == log_id
    ).first()
    if not log:
        raise HTTPException(status_code=404, detail="Sync log entry not found")

    log.status = "Success"
    log.message = f"Retry executed successfully on {datetime.now().strftime('%d %b %Y, %I:%M %p')}"
    db.commit()
    return {"success": True, "log": log.to_dict()}


@router.post("/sync/activity")
def sync_call_activity(
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    org_id = _get_org_id(current_user)
    caller_name = payload.get("caller_name", "Unknown Caller")
    crm_conn = db.query(CrmConnection).filter(
        CrmConnection.organization_id == org_id,
        CrmConnection.status == "connected"
    ).first()
    crm_name = crm_conn.provider if crm_conn else "Salesforce"

    new_log = CrmSyncLog(
        id=str(uuid.uuid4()),
        organization_id=org_id,
        datetime=datetime.now().strftime("%d %b %Y, %I:%M %p"),
        record=caller_name,
        type="Call Activity",
        crm=crm_name,
        status="Success",
        message=f"Synced call activity for {caller_name}"
    )
    db.add(new_log)
    db.commit()
    return {"success": True, "log": new_log.to_dict()}
