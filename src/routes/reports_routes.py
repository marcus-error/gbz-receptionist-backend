from typing import Optional
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.middleware.auth_middleware import get_current_user, require_permission
from src.models.user import User
from src.services.reports_service import ReportsService

router = APIRouter(prefix="/reports", tags=["Overall Reports"])


@router.get("/summary")
def get_reports_summary(
    period: Optional[str] = "this_month",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    organization_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("reports"))
):
    """Get operational report summary, call metrics, appointment metrics, and detailed tables."""
    svc = ReportsService(db)
    org_id = organization_id if current_user.role == "super_admin" else current_user.organization_id
    return svc.get_overall_report(
        organization_id=org_id,
        period=period,
        start_date=start_date,
        end_date=end_date
    )


@router.get("/export")
def export_reports_csv(
    period: Optional[str] = "this_month",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    organization_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("reports"))
):
    """Generate and stream CSV report export."""
    svc = ReportsService(db)
    org_id = organization_id if current_user.role == "super_admin" else current_user.organization_id
    csv_output = svc.generate_csv_report(
        organization_id=org_id,
        period=period,
        start_date=start_date,
        end_date=end_date
    )

    filename = f"ai_receptionist_report_{period}.csv"
    return StreamingResponse(
        iter([csv_output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
