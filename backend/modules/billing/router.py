"""Stripe billing and self-serve signup."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from config.database import get_platform_db
from config.settings import settings
from core.tenant import get_tenant_context, TenantContext
from models.platform_models import Organization, Plan, Subscription, StripeEvent, UsageCounter, User
from modules.auth.dependencies import get_current_user
from modules.auth.services import AuthService
from modules.tenants.provision import provision_tenant, validate_slug, create_tenant_tables, seed_tenant_defaults
from core.plan_limits import get_or_create_usage_counter, get_organization_plan

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Billing"])

try:
    import stripe
    if settings.STRIPE_SECRET_KEY:
        stripe.api_key = settings.STRIPE_SECRET_KEY
except ImportError:
    stripe = None


class SignupRequest(BaseModel):
    org_name: str
    org_slug: str
    plan_slug: str = "operations"
    billing_period: str = "monthly"
    admin_username: str
    admin_email: EmailStr
    admin_password: str
    admin_full_name: str


@router.post("/api/signup")
def signup(body: SignupRequest, db: Session = Depends(get_platform_db)):
    slug = validate_slug(body.org_slug)
    if AuthService.get_user_by_username(db, body.admin_username):
        raise HTTPException(status_code=400, detail="Username already exists")
    if AuthService.get_user_by_email(db, body.admin_email):
        raise HTTPException(status_code=400, detail="Email already exists")

    plan = db.query(Plan).filter(Plan.slug == body.plan_slug).first()
    if not plan:
        raise HTTPException(status_code=400, detail="Invalid plan")

    if not settings.STRIPE_SECRET_KEY or stripe is None:
        org = provision_tenant(db, slug=slug, name=body.org_name, plan_slug=body.plan_slug, status="active")
        user = AuthService.create_user(
            db, body.admin_username, body.admin_email, body.admin_password, body.admin_full_name
        )
        AuthService.add_membership(db, user.user_id, org.organization_id, "ADMIN", is_default=True)
        return {"status": "active", "organization_id": org.organization_id, "checkout_url": None}

    price_id = plan.stripe_price_monthly_id if body.billing_period == "monthly" else plan.stripe_price_annual_id
    if not price_id:
        raise HTTPException(status_code=400, detail="Plan not available for self-serve checkout")

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{settings.APP_PUBLIC_URL}/signup/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{settings.APP_PUBLIC_URL}/signup?plan={body.plan_slug}",
        metadata={
            "org_slug": slug,
            "org_name": body.org_name,
            "plan_slug": body.plan_slug,
            "admin_username": body.admin_username,
            "admin_email": str(body.admin_email),
            "admin_full_name": body.admin_full_name,
            "admin_password_hash": AuthService.hash_password(body.admin_password),
        },
    )
    pending = Organization(
        slug=slug,
        name=body.org_name,
        schema_name=f"tenant_{slug.replace('-', '_')}",
        status="pending",
        plan_id=plan.plan_id,
        stripe_customer_id=session.customer,
    )
    db.add(pending)
    db.commit()
    return {"checkout_url": session.url, "organization_id": pending.organization_id, "status": "pending"}


@router.post("/api/billing/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_platform_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    if not settings.STRIPE_WEBHOOK_SECRET or stripe is None:
        raise HTTPException(status_code=500, detail="Webhook not configured")

    try:
        event = stripe.Webhook.construct_event(payload, sig, settings.STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    existing = db.query(StripeEvent).filter(StripeEvent.stripe_event_id == event["id"]).first()
    if existing and existing.processed:
        return {"status": "already_processed"}
    if not existing:
        db.add(StripeEvent(stripe_event_id=event["id"], event_type=event["type"], payload=dict(event)))
        db.commit()

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        meta = session.get("metadata", {})
        slug = meta.get("org_slug")
        org = db.query(Organization).filter(Organization.slug == slug).first()
        if org and org.status == "pending":
            create_tenant_tables(db, org.schema_name)
            seed_tenant_defaults(db, org.schema_name, org.name)
            user = User(
                username=meta["admin_username"],
                email=meta["admin_email"],
                password_hash=meta["admin_password_hash"],
                full_name=meta["admin_full_name"],
            )
            db.add(user)
            db.flush()
            AuthService.add_membership(db, user.user_id, org.organization_id, "ADMIN", is_default=True)
            org.status = "active"
            org.stripe_customer_id = session.get("customer")
            plan = db.query(Plan).filter(Plan.slug == meta.get("plan_slug")).first()
            if plan:
                db.add(Subscription(
                    organization_id=org.organization_id,
                    plan_id=plan.plan_id,
                    stripe_subscription_id=session.get("subscription"),
                    status="active",
                ))
            db.commit()

    db.query(StripeEvent).filter(StripeEvent.stripe_event_id == event["id"]).update({"processed": True})
    db.commit()
    return {"status": "ok"}


@router.get("/api/billing/subscription")
def get_subscription(
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_platform_db),
    current_user=Depends(get_current_user),
):
    org = db.query(Organization).filter(Organization.organization_id == tenant.organization_id).first()
    plan = get_organization_plan(db, org)
    counter = get_or_create_usage_counter(db, tenant.organization_id)
    sub = db.query(Subscription).filter(Subscription.organization_id == tenant.organization_id).first()
    return {
        "plan": plan.slug if plan else None,
        "plan_name": plan.name if plan else None,
        "max_users": plan.max_users if plan else None,
        "max_documents_per_month": plan.max_documents_per_month if plan else None,
        "documents_used": counter.documents_uploaded,
        "subscription_status": sub.status if sub else org.status,
        "feature_flags": plan.feature_flags if plan else {},
    }
