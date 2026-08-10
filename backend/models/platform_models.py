"""
Platform schema models — cross-tenant identity, billing, and sessions.
"""

from datetime import datetime, date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text,
    UniqueConstraint, CheckConstraint, Index,
)
from sqlalchemy.dialects.postgresql import JSONB, INET
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from config.database import Base

PLATFORM_SCHEMA = "platform"


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = {"schema": PLATFORM_SCHEMA}

    plan_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    max_users: Mapped[int | None] = mapped_column(Integer)
    max_documents_per_month: Mapped[int | None] = mapped_column(Integer)
    price_monthly: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    price_annual: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    stripe_price_monthly_id: Mapped[str | None] = mapped_column(String(100))
    stripe_price_annual_id: Mapped[str | None] = mapped_column(String(100))
    feature_flags: Mapped[dict | None] = mapped_column(JSONB, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())

    organizations = relationship("Organization", back_populates="plan")


class Organization(Base):
    __tablename__ = "organizations"

    organization_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    schema_name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    plan_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(f"{PLATFORM_SCHEMA}.plans.plan_id"), index=True
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(String(100), index=True)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime)
    settings: Mapped[dict | None] = mapped_column(JSONB, default=dict)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    plan = relationship("Plan", back_populates="organizations")
    memberships = relationship("OrganizationMembership", back_populates="organization")
    subscriptions = relationship("Subscription", back_populates="organization")
    usage_counters = relationship("UsageCounter", back_populates="organization")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','active','trial','suspended','archived')",
            name="valid_org_status",
        ),
        {"schema": PLATFORM_SCHEMA},
    )


class User(Base):
    """Global user accounts (platform schema). Role is per-organization via memberships."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("language IN ('en', 'ur')", name="platform_valid_language"),
        {"schema": PLATFORM_SCHEMA},
    )

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone_number: Mapped[str | None] = mapped_column(String(20))
    whatsapp_number: Mapped[str | None] = mapped_column(String(20))
    active: Mapped[bool | None] = mapped_column(Boolean, default=True, index=True)
    email_verified: Mapped[bool | None] = mapped_column(Boolean, default=False)
    is_platform_admin: Mapped[bool | None] = mapped_column(Boolean, default=False)
    last_login: Mapped[datetime | None] = mapped_column(DateTime)
    login_count: Mapped[int | None] = mapped_column(Integer, default=0)
    receive_whatsapp: Mapped[bool | None] = mapped_column(Boolean, default=True)
    receive_email: Mapped[bool | None] = mapped_column(Boolean, default=True)
    language: Mapped[str | None] = mapped_column(String(10), default="en")
    timezone: Mapped[str | None] = mapped_column(String(50), default="Asia/Karachi")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(f"{PLATFORM_SCHEMA}.users.user_id")
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(f"{PLATFORM_SCHEMA}.users.user_id")
    )

    memberships = relationship("OrganizationMembership", back_populates="user")
    sessions = relationship("UserSession", back_populates="user")

    # Legacy compat: role resolved at runtime from membership + tenant Role table
    role = None


class OrganizationMembership(Base):
    __tablename__ = "organization_memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="uq_user_org"),
        {"schema": PLATFORM_SCHEMA},
    )

    membership_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(f"{PLATFORM_SCHEMA}.users.user_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    organization_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(f"{PLATFORM_SCHEMA}.organizations.organization_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    role_name: Mapped[str] = mapped_column(String(50), nullable=False, default="VIEWER")
    is_default: Mapped[bool | None] = mapped_column(Boolean, default=False)
    invited_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(f"{PLATFORM_SCHEMA}.users.user_id")
    )
    invite_token: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    invite_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="memberships", foreign_keys=[user_id])
    organization = relationship("Organization", back_populates="memberships")


class UserSession(Base):
    __tablename__ = "user_sessions"
    __table_args__ = {"schema": PLATFORM_SCHEMA}

    session_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(f"{PLATFORM_SCHEMA}.users.user_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    organization_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(f"{PLATFORM_SCHEMA}.organizations.organization_id", ondelete="CASCADE"),
        index=True,
    )
    session_token: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    login_time: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    logout_time: Mapped[datetime | None] = mapped_column(DateTime)
    active: Mapped[bool | None] = mapped_column(Boolean, default=True, index=True)

    user = relationship("User", back_populates="sessions")


class Subscription(Base):
    __tablename__ = "subscriptions"

    subscription_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(f"{PLATFORM_SCHEMA}.organizations.organization_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    plan_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(f"{PLATFORM_SCHEMA}.plans.plan_id")
    )
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    stripe_price_id: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default="trialing", index=True)
    billing_period: Mapped[str | None] = mapped_column(String(10))
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime)
    cancel_at_period_end: Mapped[bool | None] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    organization = relationship("Organization", back_populates="subscriptions")
    plan = relationship("Plan")

    __table_args__ = (
        CheckConstraint(
            "status IN ('trialing','active','past_due','canceled','unpaid')",
            name="valid_subscription_status",
        ),
        {"schema": PLATFORM_SCHEMA},
    )


class UsageCounter(Base):
    __tablename__ = "usage_counters"
    __table_args__ = (
        UniqueConstraint("organization_id", "period_start", name="uq_org_usage_period"),
        {"schema": PLATFORM_SCHEMA},
    )

    counter_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(f"{PLATFORM_SCHEMA}.organizations.organization_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    documents_uploaded: Mapped[int] = mapped_column(Integer, default=0)
    api_calls: Mapped[int] = mapped_column(Integer, default=0)
    storage_bytes: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    organization = relationship("Organization", back_populates="usage_counters")


class StripeEvent(Base):
    __tablename__ = "stripe_events"
    __table_args__ = {"schema": PLATFORM_SCHEMA}

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    stripe_event_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    payload: Mapped[Any | None] = mapped_column(JSONB)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), index=True)


class PlatformAuditLog(Base):
    __tablename__ = "platform_audit_log"
    __table_args__ = {"schema": PLATFORM_SCHEMA}

    log_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(f"{PLATFORM_SCHEMA}.users.user_id"), index=True
    )
    organization_id: Mapped[int | None] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(50))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), index=True)
