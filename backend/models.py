from pydantic import BaseModel
from typing import Optional


class PermissionRule(BaseModel):
    id: Optional[str] = None
    allowed: bool
    topics: list[str]
    description: str
    original: Optional[str] = None


class ApprovalRequest(BaseModel):
    approval_id: str
    approved: bool


class SessionConfig(BaseModel):
    name: str = "the user"
    role: str = "professional"
    context: str = "a business meeting"
    tone: str = "professional and concise"
    user_id: Optional[str] = None
    fga_role: Optional[str] = "custom"
    confidence_threshold: Optional[float] = 0.7
    language: Optional[str] = "en-US"
