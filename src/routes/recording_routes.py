from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.middleware.auth_middleware import get_current_user, require_permission
from src.models.user import User
from src.services.recording_service import RecordingService

router = APIRouter(prefix="/recordings", tags=["Call Recordings"])


class BulkDownloadRequest(BaseModel):
    period: Optional[str] = "monthly"
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    recording_ids: Optional[List[int]] = None


@router.get("")
def list_call_recordings(
    period: Optional[str] = "monthly",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    search: Optional[str] = None,
    call_type: Optional[str] = Query(None, description="Filter by call direction: 'all', 'inbound', 'outbound'"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("recordings"))
):
    """List real call recordings with period and call direction filtering and multi-tenant organization boundaries."""
    svc = RecordingService(db)
    org_id = None if current_user.role == "super_admin" else current_user.organization_id
    return svc.list_recordings(
        organization_id=org_id,
        period=period,
        start_date=start_date,
        end_date=end_date,
        search=search,
        call_type=call_type
    )


@router.get("/{recording_id}")
def get_call_recording_detail(
    recording_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("recordings"))
):
    """Get metadata details for a specific call recording."""
    svc = RecordingService(db)
    org_id = None if current_user.role == "super_admin" else current_user.organization_id
    log, disk_path = svc.get_recording_file(recording_id, organization_id=org_id)
    return {
        "id": log.id,
        "call_sid": log.call_sid,
        "organization_id": log.organization_id,
        "caller_name": log.caller_name or "Unknown",
        "caller_phone": log.caller_phone or "Unknown",
        "call_type": log.call_type.value if hasattr(log.call_type, "value") else str(log.call_type),
        "status": log.status,
        "duration": log.duration,
        "recording_status": "Available" if disk_path else (log.recording_status or "Not Recorded"),
        "recording_url": f"/api/recordings/{log.id}/audio" if disk_path else None,
        "download_url": f"/api/recordings/{log.id}/download" if disk_path else None,
        "file_size": log.recording_file_size,
        "recording_duration": log.recording_duration,
        "recording_error": log.recording_error,
        "created_at": log.created_at.isoformat() if log.created_at else None,
    }


@router.get("/{recording_id}/audio")
def stream_call_recording_audio(
    recording_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Stream call recording audio with RFC 7233 byte-range support.
    Supports seek scrubber in browser HTML5 <audio> players with HTTP 206 Partial Content.
    """
    svc = RecordingService(db)
    org_id = None if current_user.role == "super_admin" else current_user.organization_id
    range_header = request.headers.get("range")
    return svc.stream_audio(recording_id=recording_id, range_header=range_header, organization_id=org_id)


@router.get("/{recording_id}/download")
def download_individual_recording(
    recording_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Download individual finalized call recording as a standard WAV file attachment."""
    svc = RecordingService(db)
    org_id = None if current_user.role == "super_admin" else current_user.organization_id
    return svc.download_audio(recording_id=recording_id, organization_id=org_id)


@router.post("/download-bulk")
def download_bulk_recordings(
    payload: BulkDownloadRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("recordings"))
):
    """Generate and stream ZIP archive containing recordings and metadata.csv for selected time period."""
    svc = RecordingService(db)
    org_id = None if current_user.role == "super_admin" else current_user.organization_id
    zip_buffer = svc.generate_bulk_zip(
        organization_id=org_id,
        period=payload.period,
        start_date=payload.start_date,
        end_date=payload.end_date,
        recording_ids=payload.recording_ids
    )

    filename = f"call_recordings_{payload.period or 'export'}.zip"
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
