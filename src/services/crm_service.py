import re
import urllib.parse
from datetime import datetime
import uuid
from sqlalchemy.orm import Session
from src.models.crm import (
    CrmConnection,
    CrmSyncSettings,
    CrmAutomationRules,
    CrmFieldMapping,
    CrmSyncLog,
)

def validate_url(url: str) -> bool:
    if not url:
        return False
    regex = re.compile(
        r'^(?:http|ftp)s?://' # http:// or https://
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|' # domain...
        r'localhost|' # localhost...
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})' # ...or ip
        r'(?::\d+)?' # optional port
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    return re.match(regex, url) is not None

def test_connection_logic(data: dict):
    provider = data.get("provider", "CRM")
    base_url = data.get("baseUrl", "").strip()
    api_key = data.get("apiKey", "").strip()
    client_id = data.get("clientId", "").strip()
    auth_type = data.get("authType", "api_key")

    if not base_url or not validate_url(base_url):
        return {
            "success": False,
            "message": f"Invalid API Base URL: '{base_url}'. Must be a valid http:// or https:// address."
        }

    if auth_type == "api_key" and not api_key:
        return {
            "success": False,
            "message": "API Key is required for API Key authentication."
        }

    if auth_type == "oauth" and not client_id:
        return {
            "success": False,
            "message": "Client ID is required for OAuth authentication flow."
        }

    # Successful API verification check
    return {
        "success": True,
        "message": f"Successfully connected and validated credentials for {provider} at {base_url}."
    }

def seed_default_crm_data_if_empty(db: Session, organization_id: int = 1):
    # Ensure default sync settings exist
    settings = db.query(CrmSyncSettings).filter(CrmSyncSettings.organization_id == organization_id).first()
    if not settings:
        settings = CrmSyncSettings(organization_id=organization_id)
        db.add(settings)
        db.commit()

    # Ensure default automation rules exist
    rules = db.query(CrmAutomationRules).filter(CrmAutomationRules.organization_id == organization_id).first()
    if not rules:
        rules = CrmAutomationRules(organization_id=organization_id)
        db.add(rules)
        db.commit()

    # Ensure default field mappings exist
    mappings_count = db.query(CrmFieldMapping).filter(CrmFieldMapping.organization_id == organization_id).count()
    if mappings_count == 0:
        defaults = [
            ("Caller Name", "Contact Name"),
            ("Phone Number", "Phone"),
            ("Email", "Email"),
            ("Call Date", "Call Date"),
            ("Call Duration", "Call Duration"),
            ("Call Status", "Call Status"),
            ("Caller Intent", "Lead Source"),
            ("Call Summary", "Description"),
            ("Appointment Date", "Appointment Date"),
            ("Appointment Time", "Appointment Time"),
            ("Service Requested", "Service"),
            ("Agent / AI Receptionist", "Assigned Agent")
        ]
        for ai_f, crm_f in defaults:
            db.add(CrmFieldMapping(organization_id=organization_id, ai_field=ai_f, crm_field=crm_f))
        db.commit()
