"""
LME Monitoring System - Authentication Service
Multi-tenant: users in platform schema, roles per-organization via memberships.
"""

import hashlib
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from config.settings import settings
from core import security
from models.platform_models import User, OrganizationMembership, Organization
from models.database_models import Role


class AuthService:
    @staticmethod
    def hash_password(password: str) -> str:
        return security.hash_password(password)

    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        return security.verify_password(plain_password, hashed_password)

    @staticmethod
    def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
        return security.create_access_token(data, expires_delta)

    @staticmethod
    def create_pre_auth_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
        return security.create_pre_auth_token(data, expires_delta)

    @staticmethod
    def create_refresh_token(data: dict) -> str:
        return security.create_refresh_token(data)

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def decode_token(token: str) -> Optional[dict]:
        return security.decode_token(token)

    @staticmethod
    def authenticate_user(db: Session, username: str, password: str) -> Optional[User]:
        user = db.query(User).filter(User.username == username).first()
        if not user:
            return None
        if not AuthService.verify_password(password, user.password_hash):
            return None
        if not user.active:
            return None
        user.last_login = datetime.utcnow()
        user.login_count = (user.login_count or 0) + 1
        db.commit()
        return user

    @staticmethod
    def get_user_by_id(db: Session, user_id: int) -> Optional[User]:
        return db.query(User).filter(User.user_id == user_id).first()

    @staticmethod
    def get_user_by_username(db: Session, username: str) -> Optional[User]:
        return db.query(User).filter(User.username == username).first()

    @staticmethod
    def get_user_by_email(db: Session, email: str) -> Optional[User]:
        return db.query(User).filter(User.email == email).first()

    @staticmethod
    def get_user_memberships(db: Session, user_id: int) -> list[OrganizationMembership]:
        return db.query(OrganizationMembership).filter(
            OrganizationMembership.user_id == user_id
        ).all()

    @staticmethod
    def get_default_membership(db: Session, user_id: int) -> Optional[OrganizationMembership]:
        membership = db.query(OrganizationMembership).filter(
            OrganizationMembership.user_id == user_id,
            OrganizationMembership.is_default == True,
        ).first()
        if membership:
            return membership
        return db.query(OrganizationMembership).filter(
            OrganizationMembership.user_id == user_id
        ).first()

    @staticmethod
    def create_user(
        db: Session,
        username: str,
        email: str,
        password: str,
        full_name: str,
        phone_number: Optional[str] = None,
        whatsapp_number: Optional[str] = None,
        created_by: Optional[int] = None,
        is_platform_admin: bool = False,
    ) -> User:
        if AuthService.get_user_by_username(db, username):
            raise ValueError("Username already exists")
        if AuthService.get_user_by_email(db, email):
            raise ValueError("Email already exists")

        user = User(
            username=username,
            email=email,
            password_hash=AuthService.hash_password(password),
            full_name=full_name,
            phone_number=phone_number,
            whatsapp_number=whatsapp_number,
            created_by=created_by,
            is_platform_admin=is_platform_admin,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    @staticmethod
    def add_membership(
        db: Session,
        user_id: int,
        organization_id: int,
        role_name: str = "VIEWER",
        is_default: bool = False,
        invited_by: Optional[int] = None,
        invite_token: Optional[str] = None,
        invite_expires_at: Optional[datetime] = None,
    ) -> OrganizationMembership:
        existing = db.query(OrganizationMembership).filter(
            OrganizationMembership.user_id == user_id,
            OrganizationMembership.organization_id == organization_id,
        ).first()
        if existing:
            return existing

        membership = OrganizationMembership(
            user_id=user_id,
            organization_id=organization_id,
            role_name=role_name,
            is_default=is_default,
            invited_by=invited_by,
            invite_token=invite_token,
            invite_expires_at=invite_expires_at,
        )
        db.add(membership)
        db.commit()
        db.refresh(membership)
        return membership

    @staticmethod
    def check_permission_for_role(db: Session, role_name: str, permission: str) -> bool:
        role = db.query(Role).filter(Role.role_name == role_name).first()
        if not role:
            return False
        permission_map = {
            "import_lc": role.can_import_lc,
            "upload_pdf": role.can_upload_pdf,
            "view_dashboard": role.can_view_dashboard,
            "edit_lc": role.can_edit_lc,
            "delete_lc": role.can_delete_lc,
            "manage_users": role.can_manage_users,
            "configure_alerts": role.can_configure_alerts,
            "view_all_lcs": role.can_view_all_lcs,
            "export_reports": role.can_export_reports,
            "reopen_lc": role.can_reopen_lc,
            "change_lc_status": role.can_change_lc_status,
        }
        return bool(permission_map.get(permission, False))

    @staticmethod
    def check_permission(user: User, permission: str, role_name: Optional[str] = None, tenant_db: Optional[Session] = None) -> bool:
        if role_name and tenant_db:
            return AuthService.check_permission_for_role(tenant_db, role_name, permission)
        return False

    @staticmethod
    def serialize_user_payload(
        user: User,
        membership: OrganizationMembership,
        org: Organization,
        tenant_db: Session,
    ) -> dict:
        from modules.auth.dependencies import resolve_user_permissions

        permissions = resolve_user_permissions(tenant_db, membership.role_name)
        plan_slug = org.plan.slug if org.plan else None
        return {
            "user_id": user.user_id,
            "username": user.username,
            "email": user.email,
            "full_name": user.full_name,
            "role": membership.role_name,
            "permissions": permissions,
            "is_platform_admin": bool(user.is_platform_admin),
            "organization": {
                "org_id": org.organization_id,
                "slug": org.slug,
                "name": org.name,
                "plan": plan_slug,
            },
        }

    @staticmethod
    def initialize_default_users(db: Session, tenant_db: Session, organization_id: int):
        if settings.ENVIRONMENT == "production":
            print("Refusing to seed default users: ENVIRONMENT=production")
            return

        admin_role = tenant_db.query(Role).filter(Role.role_name == "ADMIN").first()
        if not admin_role:
            print("Roles not found in tenant schema.")
            return

        if not AuthService.get_user_by_username(db, "admin"):
            admin = AuthService.create_user(
                db=db,
                username="admin",
                email="admin@lme-system.com",
                password="admin123",
                full_name="System Administrator",
                phone_number="+923001234567",
                whatsapp_number="+923001234567",
                is_platform_admin=True,
            )
            AuthService.add_membership(db, admin.user_id, organization_id, "ADMIN", is_default=True)
            print(f"Created admin user: {admin.username}")

        if not AuthService.get_user_by_username(db, "manager1"):
            manager = AuthService.create_user(
                db=db,
                username="manager1",
                email="ahsan@lme-system.com",
                password="manager123",
                full_name="Ahsan (Manager)",
                phone_number="+923009876543",
                whatsapp_number="+923009876543",
            )
            AuthService.add_membership(db, manager.user_id, organization_id, "MANAGER")
            print(f"Created manager user: {manager.username}")


def get_password_hash(password: str) -> str:
    return AuthService.hash_password(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return AuthService.verify_password(plain_password, hashed_password)
