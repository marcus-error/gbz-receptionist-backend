from fastapi import APIRouter

from src.routes.appointment_routes import router as appointment_router
from src.routes.auth_routes import router as auth_router
from src.routes.call_routes import router as call_router
from src.routes.calendar_routes import router as calendar_router
from src.routes.calendar_events_routes import router as calendar_events_router
from src.routes.crm_routes import router as crm_router
from src.routes.dashboard_routes import router as dashboard_router
from src.routes.email_routes import router as email_router
from src.routes.gemini_routes import router as gemini_router
from src.routes.knowledge_routes import router as knowledge_router
from src.routes.microsoft_calendar_routes import router as microsoft_calendar_router
from src.routes.settings_routes import router as settings_router
from src.routes.workflow_routes import router as workflow_router
from src.routes.staff_routes import router as staff_router
from src.routes.department_routes import router as department_router
from src.routes.staff_availability_routes import router as staff_availability_router
from src.routes.telephony_routes import router as telephony_router
from src.routes.twilio_routes import router as twilio_router
from src.routes.twilio_webhook_routes import router as twilio_webhook_router
from src.routes.whatsapp_routes import router as whatsapp_router
from src.routes.availability_routes import router as availability_router

from src.routes.organization_routes import router as organization_router
from src.routes.user_routes import router as user_router
from src.routes.recording_routes import router as recording_router
from src.routes.reports_routes import router as reports_router
from src.routes.notification_log_routes import router as notification_log_router
from src.routes.chat_routes import router as chat_router
from src.routes.analytics_routes import router as analytics_router
from src.routes.reminder_routes import router as reminder_router
from src.routes.lead_routes import router as lead_router

api_router = APIRouter()

# Analytics routes
api_router.include_router(analytics_router)

# Multi-Tenant & RBAC routes
api_router.include_router(organization_router)
api_router.include_router(user_router)
api_router.include_router(recording_router)
api_router.include_router(reports_router)
api_router.include_router(notification_log_router)
api_router.include_router(reminder_router)
api_router.include_router(lead_router)
api_router.include_router(lead_router, prefix="/api/v1")


# Core CRUD routes
api_router.include_router(auth_router)
api_router.include_router(dashboard_router)
api_router.include_router(appointment_router)
api_router.include_router(staff_router)
api_router.include_router(department_router)
api_router.include_router(staff_availability_router)   # /{staff_id}/availability
api_router.include_router(knowledge_router)

api_router.include_router(settings_router)
api_router.include_router(workflow_router)
api_router.include_router(call_router)


# Communication integrations
api_router.include_router(crm_router)
api_router.include_router(twilio_router)
api_router.include_router(twilio_webhook_router)
api_router.include_router(email_router)
api_router.include_router(whatsapp_router)
from src.routes.webhook_routes import router as webhook_router
api_router.include_router(webhook_router)

# Calendar integrations
api_router.include_router(calendar_router)             # /calendar/*
api_router.include_router(calendar_events_router)      # /calendar/events/*
api_router.include_router(microsoft_calendar_router)   # /microsoft-calendar/*
api_router.include_router(availability_router)         # /availability/*

# AI integrations
api_router.include_router(gemini_router)
api_router.include_router(telephony_router)
api_router.include_router(chat_router)

# WebSocket / streaming routes (imported late to avoid circular deps)
from src.sockets.voice_socket import router as voice_router          # noqa: E402
from src.sockets.twilio_media_stream import router as twilio_stream_router  # noqa: E402

api_router.include_router(voice_router)
api_router.include_router(twilio_stream_router)
