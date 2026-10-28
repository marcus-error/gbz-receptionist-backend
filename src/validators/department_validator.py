from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class DepartmentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    code: Optional[str] = Field(None, max_length=50)
    description: Optional[str] = None
    head_of_department: Optional[str] = Field(None, max_length=150)
    phone: Optional[str] = Field(None, max_length=50)
    email: Optional[str] = Field(None, max_length=200)
    status: str = Field("Active", max_length=20)


class DepartmentUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    code: Optional[str] = Field(None, max_length=50)
    description: Optional[str] = None
    head_of_department: Optional[str] = Field(None, max_length=150)
    phone: Optional[str] = Field(None, max_length=50)
    email: Optional[str] = Field(None, max_length=200)
    status: Optional[str] = Field(None, max_length=20)


class DepartmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: Optional[int] = None
    name: str
    code: Optional[str] = None
    description: Optional[str] = None
    head_of_department: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    status: str = "Active"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
