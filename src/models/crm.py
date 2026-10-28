from sqlalchemy import Column, String, Integer, Boolean, DateTime, Text, ForeignKey
from datetime import datetime
import uuid

from src.config.database import Base

class CrmConnection(Base):
    __tablename__ = "crm_connections"

    id = Column(String(50), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, default=1)
    provider = Column(String(50), nullable=False)
    name = Column(String(100), nullable=False)
    account_name = Column(String(100), nullable=True)
    base_url = Column(String(255), nullable=True)
    auth_type = Column(String(30), nullable=False, default="api_key")
    
    # Sensitive credentials (stored safely, masked when returned to API response)
    api_key = Column(String(255), nullable=True)
    client_id = Column(String(255), nullable=True)
    client_secret = Column(String(255), nullable=True)
    access_token = Column(Text, nullable=True)
    refresh_token = Column(Text, nullable=True)
    
    status = Column(String(20), nullable=False, default="connected")  # connected, disconnected, syncing, error
    last_sync = Column(String(50), nullable=True, default="Just now")
    records_synced = Column(Integer, default=0)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self, mask_secrets=True):
        return {
            "id": self.id,
            "provider": self.provider,
            "name": self.name,
            "account": self.account_name or f"{self.name} Account",
            "baseUrl": self.base_url,
            "authType": self.auth_type,
            "apiKey": "••••••••••••••••" if (mask_secrets and self.api_key) else self.api_key,
            "clientId": self.client_id,
            "clientSecret": "••••••••••••••••" if (mask_secrets and self.client_secret) else self.client_secret,
            "status": self.status,
            "lastSync": self.last_sync or "Just now",
            "recordsSynced": self.records_synced or 0,
            "createdAt": self.created_at.isoformat() if self.created_at else None
        }


class CrmSyncSettings(Base):
    __tablename__ = "crm_sync_settings"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, default=1)
    sync_contacts = Column(Boolean, default=True)
    sync_leads = Column(Boolean, default=True)
    sync_call_records = Column(Boolean, default=True)
    sync_call_transcripts = Column(Boolean, default=True)
    sync_call_summary = Column(Boolean, default=True)
    sync_caller_intent = Column(Boolean, default=True)
    sync_appointments = Column(Boolean, default=True)
    sync_followup_tasks = Column(Boolean, default=True)
    sync_ai_data = Column(Boolean, default=True)
    sync_recording_url = Column(Boolean, default=True)
    sync_human_transfer_details = Column(Boolean, default=True)

    def to_dict(self):
        return {
            "syncContacts": self.sync_contacts,
            "syncLeads": self.sync_leads,
            "syncCallRecords": self.sync_call_records,
            "syncCallTranscripts": self.sync_call_transcripts,
            "syncCallSummary": self.sync_call_summary,
            "syncCallerIntent": self.sync_caller_intent,
            "syncAppointments": self.sync_appointments,
            "syncFollowupTasks": self.sync_followup_tasks,
            "syncAiData": self.sync_ai_data,
            "syncRecordingUrl": self.sync_recording_url,
            "syncHumanTransferDetails": self.sync_human_transfer_details
        }


class CrmAutomationRules(Base):
    __tablename__ = "crm_automation_rules"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, default=1)
    new_caller_contact = Column(Boolean, default=True)
    lead_identified = Column(Boolean, default=True)
    appointment_booked = Column(Boolean, default=True)
    call_transferred = Column(Boolean, default=True)
    call_unresolved = Column(Boolean, default=True)
    callback_requested = Column(Boolean, default=True)
    existing_contact_call = Column(Boolean, default=True)
    appointment_cancelled = Column(Boolean, default=True)

    def to_dict(self):
        return {
            "newCallerContact": self.new_caller_contact,
            "leadIdentified": self.lead_identified,
            "appointmentBooked": self.appointment_booked,
            "callTransferred": self.call_transferred,
            "callUnresolved": self.call_unresolved,
            "callbackRequested": self.callback_requested,
            "existingContactCall": self.existing_contact_call,
            "appointmentCancelled": self.appointment_cancelled
        }


class CrmFieldMapping(Base):
    __tablename__ = "crm_field_mappings"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, default=1)
    ai_field = Column(String(100), nullable=False)
    crm_field = Column(String(100), nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "aiField": self.ai_field,
            "crmField": self.crm_field
        }


class CrmSyncLog(Base):
    __tablename__ = "crm_sync_logs"

    id = Column(String(50), primary_key=True, default=lambda: str(uuid.uuid4()))
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, default=1)
    datetime = Column(String(50), nullable=False)
    record = Column(String(100), nullable=False)
    type = Column(String(50), nullable=False)
    crm = Column(String(50), nullable=False)
    status = Column(String(20), nullable=False, default="Success")  # Success, Pending, Failed
    message = Column(Text, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "datetime": self.datetime,
            "record": self.record,
            "type": self.type,
            "crm": self.crm,
            "status": self.status,
            "message": self.message
        }
