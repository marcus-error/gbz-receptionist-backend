"""
Webhook Event Registry.
Central registry of extensible application events that can trigger outgoing webhooks
or be received by incoming webhooks.
"""
from typing import Any, Dict, List, Optional


EVENT_REGISTRY: List[Dict[str, Any]] = [
    # Appointment Events
    {
        "event": "appointment.created",
        "category": "Appointments",
        "description": "Triggered when a new appointment is booked via AI receptionist, staff, or online sync.",
        "sample_payload": {
            "appointment_id": 101,
            "customer": "Customer Name",
            "phone": "+15552345678",
            "email": "customer@example.com",
            "department": "Care Services",
            "staff": "Staff Member",
            "service": "General Consultation",
            "appointment_date": "2026-10-15",
            "appointment_time": "10:30 AM",
            "duration": "30 Minutes",
            "status": "Confirmed",
            "notes": "Customer requested morning slot"
        }
    },
    {
        "event": "appointment.updated",
        "category": "Appointments",
        "description": "Triggered when an appointment details or timing are modified.",
        "sample_payload": {
            "appointment_id": 101,
            "customer": "Customer Name",
            "phone": "+15552345678",
            "email": "customer@example.com",
            "staff": "Staff Member",
            "appointment_date": "2026-10-16",
            "appointment_time": "02:00 PM",
            "status": "Rescheduled",
            "previous_date": "2026-10-15",
            "previous_time": "10:30 AM"
        }
    },
    {
        "event": "appointment.cancelled",
        "category": "Appointments",
        "description": "Triggered when an existing appointment is cancelled.",
        "sample_payload": {
            "appointment_id": 101,
            "customer": "Customer Name",
            "phone": "+15552345678",
            "status": "Cancelled",
            "cancellation_reason": "Customer rescheduled trip"
        }
    },
    {
        "event": "appointment.completed",
        "category": "Appointments",
        "description": "Triggered when a customer finishes their scheduled appointment.",
        "sample_payload": {
            "appointment_id": 101,
            "customer": "Customer Name",
            "staff": "Staff Member",
            "status": "Completed"
        }
    },

    # Call Events
    {
        "event": "call.started",
        "category": "Telephony",
        "description": "Triggered when an incoming or outbound phone call connects.",
        "sample_payload": {
            "call_sid": "call_9f8a7b6c5d4e3f2a",
            "caller_phone": "+15559876543",
            "caller_name": "Michael Chang",
            "direction": "inbound",
            "source": "sip_trunk",
            "timestamp": "2026-10-06T09:45:00Z"
        }
    },
    {
        "event": "call.completed",
        "category": "Telephony",
        "description": "Triggered when a call ends, including duration, AI conversation summary, and sentiment.",
        "sample_payload": {
            "call_sid": "call_9f8a7b6c5d4e3f2a",
            "caller_phone": "+15559876543",
            "caller_name": "Michael Chang",
            "direction": "inbound",
            "duration_seconds": 138,
            "hangup_reason": "REMOTE_HANGUP",
            "ai_intent": "appointment_booking",
            "summary": "Customer called to inquire about doctor availability and booked an appointment.",
            "sentiment": "Positive",
            "recording_url": "https://api.example.com/api/v1/recordings/rec_123.wav"
        }
    },

    # Lead / CRM Events
    {
        "event": "lead.created",
        "category": "CRM & Leads",
        "description": "Triggered when a new lead is captured from a phone call or chat conversation.",
        "sample_payload": {
            "lead_id": 42,
            "name": "David Miller",
            "phone": "+15558889999",
            "email": "david.m@example.com",
            "source": "AI Inbound Call",
            "status": "New",
            "notes": "Interested in premium package"
        }
    },
    {
        "event": "lead.updated",
        "category": "CRM & Leads",
        "description": "Triggered when lead status or qualification details are updated.",
        "sample_payload": {
            "lead_id": 42,
            "name": "David Miller",
            "status": "Qualified",
            "stage": "Proposal Sent"
        }
    },

    # Messaging Events
    {
        "event": "message.received",
        "category": "Messaging",
        "description": "Triggered when an incoming SMS, WhatsApp, or chat message is received.",
        "sample_payload": {
            "message_id": "msg_abc123",
            "channel": "whatsapp",
            "sender": "+15557778888",
            "text": "Hi, do you have any appointments available this Friday?",
            "timestamp": "2026-10-06T09:46:12Z"
        }
    },
    {
        "event": "message.sent",
        "category": "Messaging",
        "description": "Triggered when an automated notification or reminder is sent to a customer.",
        "sample_payload": {
            "message_id": "msg_def456",
            "channel": "email",
            "recipient": "sarah.j@example.com",
            "subject": "Appointment Confirmation - Consultation",
            "status": "Delivered"
        }
    },

    # Payment Events
    {
        "event": "payment.success",
        "category": "Payments",
        "description": "Triggered when a consultation or deposit payment succeeds.",
        "sample_payload": {
            "payment_id": "pay_987654",
            "customer_phone": "+15552345678",
            "amount": 75.00,
            "currency": "USD",
            "status": "Succeeded",
            "appointment_id": 101
        }
    },
    {
        "event": "payment.failed",
        "category": "Payments",
        "description": "Triggered when a payment attempt fails or is declined.",
        "sample_payload": {
            "payment_id": "pay_987655",
            "customer_phone": "+15552345678",
            "amount": 75.00,
            "currency": "USD",
            "status": "Failed",
            "failure_reason": "Insufficient funds"
        }
    }
]


def get_available_events() -> List[Dict[str, Any]]:
    """Return all registered webhook events with categories and sample data."""
    return list(EVENT_REGISTRY)


def get_event_names() -> List[str]:
    """Return list of valid event identifier strings."""
    return [item["event"] for item in EVENT_REGISTRY]


def get_event_sample(event_name: str) -> Dict[str, Any]:
    """Return sample payload for a specific event name."""
    for item in EVENT_REGISTRY:
        if item["event"] == event_name:
            return item["sample_payload"]
    return {
        "event": event_name,
        "message": f"Sample payload for {event_name}",
        "timestamp": "2026-10-06T09:45:00Z"
    }
