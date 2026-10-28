import logging
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_, desc, func

from src.config.database import get_db
from src.models.notification_log import NotificationLog
from src.models.appointment import Appointment
from src.models.staff import Staff

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notification-logs", tags=["Notification Logs"])


from src.middleware.auth_middleware import get_current_user
from src.models.user import User

@router.delete("/clear")
def clear_notification_logs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Clear all notification logs (for resetting to pure live logs)."""
    try:
        query = db.query(NotificationLog)
        if current_user.role != "super_admin":
            query = query.filter(NotificationLog.organization_id == current_user.organization_id)
        deleted_count = query.delete(synchronize_session=False)
        db.commit()
        return {"message": "Notification logs cleared successfully", "deleted": deleted_count}
    except Exception as exc:
        db.rollback()
        logger.error(f"Failed to clear notification logs: {exc}")
        return {"message": "Failed to clear notification logs", "error": str(exc)}


@router.get("")
def list_notification_logs(
    recipient_type: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(NotificationLog)
    if current_user.role != "super_admin":
        query = query.filter(NotificationLog.organization_id == current_user.organization_id)

    if recipient_type and recipient_type != "all":
        query = query.filter(NotificationLog.recipient_type == recipient_type)

    if channel and channel != "all":
        query = query.filter(NotificationLog.channel == channel)

    if action and action != "all":
        if action == "reminder":
            query = query.filter(NotificationLog.action.ilike("%reminder%"))
        else:
            query = query.filter(NotificationLog.action == action)

    if status and status != "all":
        query = query.filter(NotificationLog.status == status)

    if search:
        search_fmt = f"%{search.strip()}%"
        query = query.filter(
            or_(
                NotificationLog.recipient_name.ilike(search_fmt),
                NotificationLog.recipient_contact.ilike(search_fmt),
                NotificationLog.subject.ilike(search_fmt),
                NotificationLog.message_body.ilike(search_fmt),
            )
        )

    total = query.count()
    logs = query.order_by(desc(NotificationLog.created_at)).offset(offset).limit(limit).all()

    # Calculate overall stats scoped to organization
    stats_base = db.query(NotificationLog)
    if current_user.role != "super_admin":
        stats_base = stats_base.filter(NotificationLog.organization_id == current_user.organization_id)

    total_sent = stats_base.filter(NotificationLog.status == "sent").count()
    customer_count = stats_base.filter(NotificationLog.recipient_type == "customer").count()
    staff_count = stats_base.filter(NotificationLog.recipient_type == "staff").count()
    whatsapp_count = stats_base.filter(NotificationLog.channel == "whatsapp").count()
    email_count = stats_base.filter(NotificationLog.channel == "email").count()


    items = []
    for item in logs:
        items.append({
            "id": item.id,
            "appointment_id": item.appointment_id,
            "recipient_type": item.recipient_type,
            "recipient_name": item.recipient_name,
            "recipient_contact": item.recipient_contact,
            "channel": item.channel,
            "action": item.action,
            "subject": item.subject,
            "message_body": item.message_body,
            "status": item.status,
            "error_reason": item.error_reason,
            "created_at": item.created_at.isoformat() if item.created_at else None,
        })

    return {
        "total": total,
        "logs": items,
        "stats": {
            "total_sent": total_sent,
            "customer_count": customer_count,
            "staff_count": staff_count,
            "whatsapp_count": whatsapp_count,
            "email_count": email_count,
        }
    }
