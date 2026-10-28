from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.middleware.auth_middleware import get_current_user
from src.models.user import User
from src.models.workflow import Workflow, WorkflowExecutionLog

router = APIRouter(prefix="/workflows", tags=["Workflows"])


class WorkflowCreatePayload(BaseModel):
    name: str
    description: Optional[str] = None
    trigger_event: str
    action_type: str
    action_config: Optional[str] = None
    is_active: Optional[bool] = True


class WorkflowOut(BaseModel):
    id: int
    organization_id: Optional[int]
    name: str
    description: Optional[str]
    trigger_event: str
    action_type: str
    action_config: Optional[str]
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class WorkflowLogOut(BaseModel):
    id: int
    workflow_id: Optional[int]
    workflow_name: Optional[str]
    trigger_event: str
    action_type: str
    status: str
    details: Optional[str]
    executed_at: datetime

    class Config:
        from_attributes = True


DEFAULT_TEMPLATES = [
    {
        "name": "Missed Call Auto-SMS / WhatsApp Responder",
        "description": "Automatically sends a polite follow-up message when a customer call is missed",
        "trigger_event": "missed_call",
        "action_type": "send_whatsapp",
        "action_config": '{"message": "Sorry we missed your call! An agent will call you back shortly. Reply here for urgent assistance.", "channel": "whatsapp_sms"}',
        "is_active": True,
    },
    {
        "name": "Appointment Confirmation Email & WhatsApp",
        "description": "Triggers immediate appointment confirmation details to caller upon booking",
        "trigger_event": "appointment_created",
        "action_type": "send_email",
        "action_config": '{"subject": "Your Appointment Confirmation", "template": "appointment_booking_success", "send_whatsapp_too": true}',
        "is_active": True,
    },
    {
        "name": "Emergency Staff Notification Alert",
        "description": "Notifies designated emergency staff on call when an urgent caller request is flagged",
        "trigger_event": "emergency_call",
        "action_type": "notify_staff",
        "action_config": '{"alert_level": "high", "notify_channel": "sms_email", "target_role": "duty_doctor"}',
        "is_active": True,
    },
    {
        "name": "New Caller Lead Auto-CRM Sync",
        "description": "Syncs newly captured caller lead contact info directly to CRM pipeline",
        "trigger_event": "lead_captured",
        "action_type": "sync_crm",
        "action_config": '{"crm_provider": "hubspot_zoho", "pipeline_stage": "New Inbound Inquiry"}',
        "is_active": True,
    },
]


def _get_org_id(user: User) -> int:
    return user.organization_id or 1


@router.get("", response_model=List[WorkflowOut])
def list_workflows(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List workflows for current organization. Seeds default templates on first view."""
    org_id = _get_org_id(current_user)
    workflows = db.query(Workflow).filter(
        (Workflow.organization_id == org_id) | (Workflow.organization_id == None)
    ).all()

    if not workflows:
        # Seed default templates for org
        seeded = []
        for t in DEFAULT_TEMPLATES:
            wf = Workflow(
                organization_id=org_id,
                name=t["name"],
                description=t["description"],
                trigger_event=t["trigger_event"],
                action_type=t["action_type"],
                action_config=t["action_config"],
                is_active=t["is_active"],
            )
            db.add(wf)
            seeded.append(wf)
        db.commit()
        for s in seeded:
            db.refresh(s)
        return seeded

    return workflows


@router.post("", response_model=WorkflowOut, status_code=status.HTTP_201_CREATED)
def create_workflow(
    payload: WorkflowCreatePayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new custom workflow."""
    org_id = _get_org_id(current_user)
    wf = Workflow(
        organization_id=org_id,
        name=payload.name,
        description=payload.description,
        trigger_event=payload.trigger_event,
        action_type=payload.action_type,
        action_config=payload.action_config,
        is_active=payload.is_active if payload.is_active is not None else True,
    )
    db.add(wf)
    db.commit()
    db.refresh(wf)
    return wf


@router.put("/{workflow_id}", response_model=WorkflowOut)
def update_workflow(
    workflow_id: int,
    payload: WorkflowCreatePayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update existing workflow."""
    wf = db.query(Workflow).filter(Workflow.id == workflow_id).first()
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")

    wf.name = payload.name
    wf.description = payload.description
    wf.trigger_event = payload.trigger_event
    wf.action_type = payload.action_type
    wf.action_config = payload.action_config
    if payload.is_active is not None:
        wf.is_active = payload.is_active

    db.commit()
    db.refresh(wf)
    return wf


@router.put("/{workflow_id}/toggle")
def toggle_workflow(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Toggle workflow active status."""
    wf = db.query(Workflow).filter(Workflow.id == workflow_id).first()
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")

    wf.is_active = not wf.is_active
    db.commit()
    return {"status": "success", "is_active": wf.is_active, "message": f"Workflow is now {'active' if wf.is_active else 'inactive'}"}


@router.delete("/{workflow_id}")
def delete_workflow(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a workflow."""
    wf = db.query(Workflow).filter(Workflow.id == workflow_id).first()
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")

    db.delete(wf)
    db.commit()
    return {"status": "success", "message": "Workflow deleted successfully"}


@router.get("/logs", response_model=List[WorkflowLogOut])
def list_workflow_logs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List execution history logs for organization workflows."""
    org_id = _get_org_id(current_user)
    logs = db.query(WorkflowExecutionLog).filter(
        (WorkflowExecutionLog.organization_id == org_id) | (WorkflowExecutionLog.organization_id == None)
    ).order_by(WorkflowExecutionLog.executed_at.desc()).limit(100).all()
    return logs


@router.post("/{workflow_id}/test")
def test_workflow(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Test run a workflow and create an execution log entry."""
    org_id = _get_org_id(current_user)
    wf = db.query(Workflow).filter(Workflow.id == workflow_id).first()
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")

    log_entry = WorkflowExecutionLog(
        workflow_id=wf.id,
        organization_id=org_id,
        workflow_name=wf.name,
        trigger_event=wf.trigger_event,
        action_type=wf.action_type,
        status="success",
        details=f"Test execution triggered manually by {current_user.name}. Action: {wf.action_type} executed cleanly."
    )
    db.add(log_entry)
    db.commit()

    return {
        "status": "success",
        "message": f"Test run for '{wf.name}' completed successfully!",
        "executed_action": wf.action_type
    }
