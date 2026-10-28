from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.models.gemini_account import GeminiAccount

router = APIRouter(prefix="/gemini/accounts", tags=["Gemini"])

class GeminiAccountSchema(BaseModel):
    id: Optional[int] = None
    account: str
    status: Optional[str] = "Active"
    requests: Optional[str] = "0"
    quota: Optional[str] = "0%"
    api_key: Optional[str] = None

@router.get("", response_model=List[GeminiAccountSchema])
def get_gemini_accounts(db: Session = Depends(get_db)):
    accounts = db.query(GeminiAccount).all()
    if not accounts:
        accounts = []
        
    return [
        GeminiAccountSchema(
            id=acc.id,
            account=acc.account,
            status=acc.status,
            requests=acc.requests,
            quota=acc.quota,
            api_key=acc.api_key
        )
        for acc in accounts
    ]

@router.post("", response_model=GeminiAccountSchema)
def add_gemini_account(payload: GeminiAccountSchema, db: Session = Depends(get_db)):
    new_acc = GeminiAccount(
        account=payload.account,
        status="Active",
        requests="0",
        quota="0%",
        api_key=payload.api_key
    )
    db.add(new_acc)
    db.commit()
    db.refresh(new_acc)
    
    return GeminiAccountSchema(
        id=new_acc.id,
        account=new_acc.account,
        status=new_acc.status,
        requests=new_acc.requests,
        quota=new_acc.quota,
        api_key=new_acc.api_key
    )
