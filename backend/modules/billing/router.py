"""Stripe billing and self-serve signup."""

import logging
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from config.database import get_platform_db
from config.settings import settings
from core.rate_limit import limiter
from core.redis import redis_cache
from core.tenant import get_tenant_context, TenantContext
from models.platform_models import Organization, Plan, Subscription, StripeEvent, User
from modules.auth.dependencies import get_current_user
from modules.auth.services import AuthService
from modules.tenants.provision import provision_tenant, validate_slug, create_tenant_tables, seed_tenant_defaults
from core.plan_limits import get_or_create_usage_counter, get_organization_plan
from infrastructure.email.mailer import send_email

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Billing"])

SIGNUP_SECRET_TTL = 48 * 3600

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


def _signup_secret_key(slug: str) -> str:
    return f"lme:signup:{slug}"


def _store_signup_secret(slug: str, password: str) -> None:
    redis_cache.set(_signup_secret_key(slug), AuthService.hash_password(password), ex=SIGNUP_SECRET_TTL)


def _pop_signup_secret(slug: str) -> str | None:
    key = _signup_secret_key(slug)
    hashed = redis_cache.get(key)
    if hashed:
        redis_cache.delete(key)
    return hashed


@router.post("/api/signup")
@limiter.limit(settings.SIGNUP_RATE_LIMIT)
def signup(body: SignupRequest, request: Request, db: Session = Depends(get_platform_db)):
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

    customer = stripe.Customer.create(
        email=str(body.admin_email),
        name=body.org_name,
        metadata={"org_slug": slug},
    )
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer.id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{settings.APP_PUBLIC_URL}/signup/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{settings.APP_PUBLIC_URL}/signup?plan={body.plan_slug}",
        metadata={
            "org_slug": slug,
            "org_name": body.org_name,
            "plan_slug": body.plan_slug,
        },
    )
    _store_signup_secret(slug, body.admin_password)
    pending = Organization(
        slug=slug,
        name=body.org_name,
        schema_name=f"tenant_{slug.replace('-', '_')}",
        status="pending",
        plan_id=plan.plan_id,
        stripe_customer_id=customer.id,
        settings={
            "pending_signup": {
                "admin_username": body.admin_username,
                "admin_email": str(body.admin_email),
                "admin_full_name": body.admin_full_name,
                "plan_slug": body.plan_slug,
                "created_at": datetime.utcnow().isoformat(),
            }
        },
    )
    db.add(pending)
    db.commit()
    return {"checkout_url": session.url, "organization_id": pending.organization_id, "status": "pending"}


def _activate_pending_org(db: Session, org: Organization, meta: dict, stripe_sub_id: str | None, stripe_customer_id: str | None) -> None:
    signup = (org.settings or {}).get("pending_signup") or {}
    if not signup.get("admin_username"):
        logger.error("Pending org %s missing pending_signup settings", org.slug)
        return

    create_tenant_tables(db, org.schema_name)
    seed_tenant_defaults(db, org.schema_name, org.name)

    password_hash = _pop_signup_secret(org.slug)
    generated_password = False
    if not password_hash:
        generated_password = True
        password_hash = AuthService.hash_password(secrets.token_urlsafe(24))
        logger.warning("Signup secret missing for %s — admin must reset password", org.slug)

    user = User(
        username=signup["admin_username"],
        email=signup["admin_email"],
        password_hash=password_hash,
        full_name=signup["admin_full_name"],
    )
    db.add(user)
    db.flush()
    AuthService.add_membership(db, user.user_id, org.organization_id, "ADMIN", is_default=True)
    org.status = "active"
    if stripe_customer_id:
        org.stripe_customer_id = stripe_customer_id
    org.settings = {k: v for k, v in (org.settings or {}).items() if k != "pending_signup"}
    plan = db.query(Plan).filter(Plan.slug == meta.get("plan_slug") or signup.get("plan_slug")).first()
    if plan:
        db.add(Subscription(
            organization_id=org.organization_id,
            plan_id=plan.plan_id,
            stripe_subscription_id=stripe_sub_id,
            status="active",
        ))
    if generated_password:
        send_email(
            signup["admin_email"],
            "Set your password",
            f"Your {org.name} workspace is ready. Use Forgot password at {settings.APP_PUBLIC_URL}/forgot-password to choose a password.",
        )


def _sync_subscription_status(db: Session, org: Organization, stripe_status: str) -> None:
    sub = db.query(Subscription).filter(Subscription.organization_id == org.organization_id).first()
    if sub:
        sub.status = stripe_status
    if stripe_status in ("canceled", "unpaid"):
        org.status = "suspended"
    elif stripe_status == "past_due":
        if sub:
            sub.status = "past_due"
    elif stripe_status in ("active", "trialing"):
        org.status = "active"


def _org_by_customer(db: Session, customer_id: str | None) -> Organization | None:
    if not customer_id:
        return None
    return db.query(Organization).filter(Organization.stripe_customer_id == customer_id).first()


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

    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type == "checkout.session.completed":
        meta = obj.get("metadata", {}) or {}
        slug = meta.get("org_slug")
        customer_id = obj.get("customer")
        org = db.query(Organization).filter(Organization.slug == slug).first()
        if org:
            if customer_id:
                org.stripe_customer_id = customer_id
            if org.status == "pending":
                _activate_pending_org(db, org, meta, obj.get("subscription"), customer_id)
            db.commit()

    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub_id = obj.get("id")
        customer_id = obj.get("customer")
        sub_row = db.query(Subscription).filter(Subscription.stripe_subscription_id == sub_id).first()
        org = None
        if sub_row:
            org = db.query(Organization).filter(Organization.organization_id == sub_row.organization_id).first()
        if not org:
            org = _org_by_customer(db, customer_id)
        if org:
            if customer_id:
                org.stripe_customer_id = customer_id
            status = obj.get("status", "canceled")
            if event_type == "customer.subscription.deleted":
                status = "canceled"
            _sync_subscription_status(db, org, status)
            if sub_row:
                sub_row.status = status
            db.commit()

    elif event_type == "invoice.payment_failed":
        org = _org_by_customer(db, obj.get("customer"))
        if org:
            org.status = "suspended"
            sub = db.query(Subscription).filter(Subscription.organization_id == org.organization_id).first()
            if sub:
                sub.status = "past_due"
            db.commit()
            logger.warning("Payment failed — suspended org %s", org.slug)

    elif event_type in ("invoice.paid", "invoice.payment_succeeded"):
        org = _org_by_customer(db, obj.get("customer"))
        if org and org.status == "suspended":
            org.status = "active"
            sub = db.query(Subscription).filter(Subscription.organization_id == org.organization_id).first()
            if sub:
                sub.status = "active"
            db.commit()
            logger.info("Payment succeeded — reactivated org %s", org.slug)

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
        "has_stripe_customer": bool(org.stripe_customer_id),
    }


@router.post("/api/billing/portal")
def billing_portal(
    tenant: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_platform_db),
    current_user=Depends(get_current_user),
):
    if stripe is None or not settings.STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Billing portal is not configured")
    org = db.query(Organization).filter(Organization.organization_id == tenant.organization_id).first()
    if not org or not org.stripe_customer_id:
        raise HTTPException(status_code=400, detail="No Stripe customer on this organization")
    session = stripe.billing_portal.Session.create(
        customer=org.stripe_customer_id,
        return_url=f"{settings.APP_PUBLIC_URL}/settings/billing",
    )
    return {"url": session.url}
