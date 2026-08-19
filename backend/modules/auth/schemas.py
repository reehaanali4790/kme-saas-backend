"""
Pydantic schemas for User Authentication
"""
from typing import Optional, List, Any

from pydantic import BaseModel, EmailStr


class OrganizationSummary(BaseModel):
    org_id: int
    slug: str
    name: str
    role: str
    plan: Optional[str] = None


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    user: Optional[dict] = None
    csrf_token: Optional[str] = None
    organizations: Optional[List[Any]] = None
    requires_org_selection: bool = False


class UserResponse(BaseModel):
    user_id: int
    username: str
    email: str
    full_name: str
    role_name: str
    active: bool

    class Config:
        from_attributes = True


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class RefreshRequest(BaseModel):
    refresh_token: Optional[str] = None


class SelectOrgRequest(BaseModel):
    org_id: int


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class AcceptInviteRequest(BaseModel):
    token: str
    password: str
