import logging
from typing import List, Optional, Dict, Any
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.middleware.auth_middleware import get_current_user
from src.models.user import User
from src.models.lead import Lead
from src.services.lead_service import LeadService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/leads", tags=["Leads"])


class LeadCreateSchema(BaseModel):
    name: Optional[str] = "Unknown Lead"
    phone: str
    call_time: datetime
    script: Optional[str] = None


class LeadBulkUploadSchema(BaseModel):
    leads: List[LeadCreateSchema]


class LeadUpdateSchema(BaseModel):
    lead_status: Optional[str] = None
    appointment_status: Optional[str] = None
    notes: Optional[str] = None
    follow_up_required: Optional[bool] = None
    follow_up_date: Optional[str] = None
    product_service: Optional[str] = None


class LeadActivityCreateSchema(BaseModel):
    event_type: str = Field(..., example="note_added")
    title: str = Field(..., example="Manual Follow-up Note")
    description: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


@router.get("/metrics")
def get_leads_metrics(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retrieve dynamic summary card metrics calculated from real database records."""
    org_id = current_user.organization_id or 1
    svc = LeadService(db)
    return svc.get_metrics(org_id)


@router.get("/services")
def get_leads_services(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retrieve distinct list of products/services discussed or booked."""
    org_id = current_user.organization_id or 1
    svc = LeadService(db)
    return svc.get_services(org_id)


@router.post("/sync")
def sync_leads_from_history(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Synchronize existing calls and appointments into the Leads table."""
    svc = LeadService(db)
    result = svc.backfill_historical_data()
    return {"message": "Sync completed successfully", "details": result}


@router.get("")
def list_leads(
    search: Optional[str] = Query(None, description="Search by customer name, phone, or email"),
    call_type: Optional[str] = Query(None, description="Inbound or Outbound"),
    lead_status: Optional[str] = Query(None, description="New, Contacted, Qualified, Converted, Lost"),
    appointment_status: Optional[str] = Query(None, description="Not Booked, Pending, Booked, Completed, Cancelled"),
    product_service: Optional[str] = Query(None, description="Product or service filter"),
    date_range: Optional[str] = Query(None, description="today, 7days, 30days"),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    sort_by: str = Query("call_time", description="Field to sort by"),
    sort_order: str = Query("desc", description="asc or desc"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, le=500),
    offset: Optional[int] = Query(None),  # Backward compatibility
    status: Optional[str] = Query(None),  # Backward compatibility for dialer
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List leads with multi-filter, search, sorting and server-side pagination."""
    org_id = current_user.organization_id or 1
    
    # Handle backward compatibility with dialer page offset parameter
    if offset is not None and offset > 0 and page == 1:
        page = (offset // limit) + 1

    svc = LeadService(db)
    return svc.get_leads(
        organization_id=org_id,
        search=search,
        call_type=call_type,
        lead_status=lead_status,
        appointment_status=appointment_status,
        product_service=product_service,
        date_range=date_range,
        start_date=start_date,
        end_date=end_date,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        limit=limit,
        status=status,
    )


@router.get("/{lead_id}")
def get_lead_details(
    lead_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch complete end-to-end history for a specific lead."""
    org_id = current_user.organization_id or 1
    svc = LeadService(db)
    details = svc.get_lead_details(lead_id, org_id)
    if not details:
        raise HTTPException(status_code=404, detail="Lead not found")
    return details


@router.patch("/{lead_id}")
def update_lead(
    lead_id: int,
    payload: LeadUpdateSchema,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update lead status, appointment status, follow-up, or notes."""
    org_id = current_user.organization_id or 1
    svc = LeadService(db)
    updated = svc.update_lead(lead_id, org_id, payload.model_dump(exclude_unset=True))
    if not updated:
        raise HTTPException(status_code=404, detail="Lead not found")
    return updated


@router.post("/{lead_id}/activities")
def add_lead_activity(
    lead_id: int,
    payload: LeadActivityCreateSchema,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Append a timeline activity event or note to the lead."""
    org_id = current_user.organization_id or 1
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.organization_id == org_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    svc = LeadService(db)
    act = svc.add_activity(
        lead_id=lead_id,
        organization_id=org_id,
        event_type=payload.event_type,
        title=payload.title,
        description=payload.description,
        metadata=payload.metadata,
    )
    return {
        "id": act.id,
        "event_type": act.event_type,
        "title": act.title,
        "description": act.description,
        "created_at": act.created_at.isoformat() if act.created_at else None,
    }


# ==============================================================================
# Preserved Endpoints for Automated Dialer Campaigns
# ==============================================================================

@router.post("/upload")
def upload_leads(
    payload: LeadBulkUploadSchema,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Bulk upload leads for the automated dialer."""
    created = []
    org_id = current_user.organization_id or 1
    try:
        for item in payload.leads:
            lead = Lead(
                organization_id=org_id,
                name=item.name,
                phone=item.phone,
                call_time=item.call_time,
                script=item.script,
                status="pending",
                lead_status="New",
                appointment_status="Not Booked",
                call_type="Outbound",
                lead_source="Outbound Campaign",
                product_service="Outbound Campaign",
            )
            db.add(lead)
            db.flush()
            lead.lead_number = f"LEAD-{lead.id:04d}"
            created.append(lead)
        db.commit()
        return {"message": f"Successfully uploaded {len(created)} leads.", "count": len(created)}
    except Exception as e:
        db.rollback()
        logger.error(f"[Leads API] Upload failed: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to upload leads: {e}")


@router.delete("/{lead_id}")
def delete_lead(
    lead_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cancel or delete a lead."""
    org_id = current_user.organization_id or 1
    lead = db.query(Lead).filter(
        Lead.id == lead_id, 
        Lead.organization_id == org_id
    ).first()
    
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
        
    db.delete(lead)
    db.commit()
    return {"message": "Lead deleted successfully"}
