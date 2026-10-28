from typing import List, Optional
from sqlalchemy.orm import Session

from src.models.call_log import CallLog, CallType


class CallRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_all(self, organization_id: Optional[int] = None) -> List[CallLog]:
        query = self.db.query(CallLog)
        if organization_id is not None:
            query = query.filter(CallLog.organization_id == organization_id)
        return query.order_by(CallLog.id.desc()).all()

    def get_latest_by_type(self, call_type: CallType, organization_id: Optional[int] = None) -> Optional[CallLog]:
        query = self.db.query(CallLog)
        if organization_id is not None:
            query = query.filter(CallLog.organization_id == organization_id)
        if call_type:
            query = query.filter(CallLog.call_type == call_type)
        return query.order_by(CallLog.id.desc()).first()

    def get_by_id(self, call_id: int, organization_id: Optional[int] = None) -> Optional[CallLog]:
        query = self.db.query(CallLog).filter(CallLog.id == call_id)
        if organization_id is not None:
            query = query.filter(CallLog.organization_id == organization_id)
        return query.first()

    def create(self, data: dict) -> CallLog:
        call = CallLog(**data)
        self.db.add(call)
        self.db.commit()
        self.db.refresh(call)
        return call

    def get_by_sid(self, call_sid: str) -> Optional[CallLog]:
        return self.db.query(CallLog).filter(CallLog.call_sid == call_sid).first()

    def update_by_sid(self, call_sid: str, data: dict) -> Optional[CallLog]:
        call = self.get_by_sid(call_sid)
        if call:
            for key, value in data.items():
                setattr(call, key, value)
            self.db.commit()
            self.db.refresh(call)
        return call
