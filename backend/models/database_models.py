"""
LME Monitoring System - Database Models
Version: 2.0
Based on PostgreSQL schema v2.0
"""

from sqlalchemy import Integer, String, Boolean, Date, DateTime, Numeric, Text, Time, ARRAY, ForeignKey, CheckConstraint, UniqueConstraint, Index, text
from sqlalchemy.dialects.postgresql import JSONB, INET
from sqlalchemy.orm import relationship, Mapped, mapped_column
from sqlalchemy.sql import func
from datetime import datetime, date, time
from decimal import Decimal
from typing import cast, Any
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import Base, PLATFORM_SCHEMA, SHARED_SCHEMA

# Platform identity models (re-exported for backward-compatible imports)
from models.platform_models import User, UserSession

class Role(Base):
    """User roles with permissions"""
    __tablename__ = "roles"
    
    role_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    role_name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    role_description: Mapped[str | None] = mapped_column(Text)
    
    # Permissions
    can_import_lc: Mapped[bool | None] = mapped_column(Boolean, default=False)
    can_upload_pdf: Mapped[bool | None] = mapped_column(Boolean, default=False)
    can_view_dashboard: Mapped[bool | None] = mapped_column(Boolean, default=True)
    can_edit_lc: Mapped[bool | None] = mapped_column(Boolean, default=False)
    can_delete_lc: Mapped[bool | None] = mapped_column(Boolean, default=False)
    can_manage_users: Mapped[bool | None] = mapped_column(Boolean, default=False)
    can_configure_alerts: Mapped[bool | None] = mapped_column(Boolean, default=False)
    can_view_all_lcs: Mapped[bool | None] = mapped_column(Boolean, default=True)
    can_export_reports: Mapped[bool | None] = mapped_column(Boolean, default=False)
    can_reopen_lc: Mapped[bool | None] = mapped_column(Boolean, default=False)
    can_change_lc_status: Mapped[bool | None] = mapped_column(Boolean, default=False)
    
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    
    # Relationships — roles are tenant-scoped; users live in platform schema

class CalculationFormula(Base):
    """LME calculation formulas"""
    __tablename__ = "calculation_formulas"
    
    formula_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    formula_number: Mapped[int] = mapped_column(Integer, nullable=False)
    origin: Mapped[str] = mapped_column(String(50), nullable=False)
    region: Mapped[str] = mapped_column(String(10), nullable=False)
    quality: Mapped[str] = mapped_column(String(20), nullable=False)
    products: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    discount_type: Mapped[str | None] = mapped_column(String(10))
    discount_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    freight_base: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    freight_additional: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=0)
    eur_usd_conversion: Mapped[bool | None] = mapped_column(Boolean, default=False)
    multi_source: Mapped[bool | None] = mapped_column(Boolean, default=False)
    formula_description: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())

class LCMaster(Base):
    """Main LC information"""
    __tablename__ = "lc_master"
    
    lc_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    lc_number: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    contract_number: Mapped[str | None] = mapped_column(String(100))
    contract_date: Mapped[date | None] = mapped_column(Date)
    supplier_name: Mapped[str | None] = mapped_column(String(300))
    importer_name: Mapped[str | None] = mapped_column(String(300))
    lc_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, index=True)
    last_ship_date: Mapped[date | None] = mapped_column(Date, index=True)
    currency: Mapped[str | None] = mapped_column(String(20))
    exchange_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    hoa: Mapped[str | None] = mapped_column(String(200))
    # Clause-like fields carry whole sentences off the SWIFT LC — no business limit,
    # so TEXT: truncating real LC wording would lose contractual detail.
    payment_terms: Mapped[str | None] = mapped_column(Text)
    delivery_terms: Mapped[str | None] = mapped_column(Text)
    bank_name: Mapped[str | None] = mapped_column(String(300))
    booked_by: Mapped[str | None] = mapped_column(String(200))          # LEGACY free-text buyer/booked-by (kept as-is)
    # Structured buyer / booked-by allocation (new). NULL type = legacy row (use booked_by
    # text). SINGLE = 100% to one buyer; MULTIPLE = split across lc_buyer_allocations rows.
    buyer_allocation_type: Mapped[str | None] = mapped_column(String(10))    # 'SINGLE' | 'MULTIPLE' | None
    buyer_allocation_basis: Mapped[str | None] = mapped_column(String(12))   # 'PERCENTAGE' | 'QUANTITY' | None
    indentor: Mapped[str | None] = mapped_column(String(300))
    insurance: Mapped[str | None] = mapped_column(Text)
    reimbursement: Mapped[str | None] = mapped_column(String(50))      # W/O | May Add | Confirm (manual select)
    # Shipment tracking
    vessel_name: Mapped[str | None] = mapped_column(String(300))
    bl_number: Mapped[str | None] = mapped_column(String(100))
    ship_on_board: Mapped[date | None] = mapped_column(Date)
    eta: Mapped[date | None] = mapped_column(Date)
    arrival_port: Mapped[str | None] = mapped_column(String(200))
    invoice_number: Mapped[str | None] = mapped_column(String(100))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    doc_payment_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    doc_payment_date: Mapped[date | None] = mapped_column(Date)
    bank_int_date: Mapped[date | None] = mapped_column(Date)
    delivery_date: Mapped[date | None] = mapped_column(Date)
    shipped_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    
    # Monitoring fields
    monitoring_expiry: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # days_remaining is computed
    
    # Status
    status: Mapped[str | None] = mapped_column(String(20), default='OPEN', index=True)
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    status_changed_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))
    status_notes: Mapped[str | None] = mapped_column(Text)
    
    # Manual actions
    reopen_requested: Mapped[bool | None] = mapped_column(Boolean, default=False)
    reopen_requested_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))
    reopen_requested_at: Mapped[datetime | None] = mapped_column(DateTime)
    reopen_notes: Mapped[str | None] = mapped_column(Text)
    
    # Ownership
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))
    assigned_to: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'), index=True)

    # Origin: contract-created (the new wizard) vs legacy Excel/old-software import
    source: Mapped[str | None] = mapped_column(String(20), default='LEGACY_IMPORT')   # 'CONTRACT' | 'LEGACY_IMPORT'
    contract_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('contracts.contract_id', ondelete='SET NULL'),
                         nullable=True, index=True)
    # LC document (SWIFT MT700) for contract-created LCs
    document_path: Mapped[str | None] = mapped_column(Text)
    raw_extracted_data: Mapped[Any | None] = mapped_column(JSONB)

    # Timestamps
    import_date: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    last_updated: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    products = relationship("LCProduct", back_populates="lc", cascade="all, delete-orphan")
    buyer_allocations = relationship("LCBuyerAllocation", back_populates="lc",
                                     cascade="all, delete-orphan")
    alerts = relationship("PriceAlert", back_populates="lc", cascade="all, delete-orphan")
    bill_of_ladings = relationship("BillOfLading", back_populates="lc")
    shipments = relationship("Shipment", back_populates="lc")
    creator = relationship("User", foreign_keys=[created_by])
    assignee = relationship("User", foreign_keys=[assigned_to])
    contract = relationship("Contract", back_populates="lc", foreign_keys=[contract_id])
    
    __table_args__ = (
        CheckConstraint("status IN ('OPEN', 'CLOSED', 'SHIPPED', 'EXPIRED', 'CANCELLED')", name='valid_status'),
        # Every report/dashboard filter combines a status check with a lc_date range —
        # a lone status index can't serve that combo without a second table lookup.
        Index('idx_lcmaster_status_date', 'status', 'lc_date'),
        # lc_number/importer_name/vessel_name are all searched via leading-wildcard
        # ilike('%term%') across the reports and LC-table endpoints — a plain B-tree index
        # can't serve that pattern at all. A trigram GIN index lets Postgres use an index
        # scan for it instead of a full sequential scan (requires the pg_trgm extension —
        # see scripts/migrations/migrate_trgm_search_indexes.py).
        Index('idx_lcmaster_lc_number_trgm', 'lc_number',
              postgresql_using='gin', postgresql_ops={'lc_number': 'gin_trgm_ops'}),
        Index('idx_lcmaster_importer_trgm', 'importer_name',
              postgresql_using='gin', postgresql_ops={'importer_name': 'gin_trgm_ops'}),
        Index('idx_lcmaster_vessel_trgm', 'vessel_name',
              postgresql_using='gin', postgresql_ops={'vessel_name': 'gin_trgm_ops'}),
    )

    @property
    def days_remaining(self):
        """Calculate days remaining"""
        expiry = cast(date | None, self.monitoring_expiry)
        if expiry:
            delta = expiry - date.today()
            return delta.days
        return None

class LCProduct(Base):
    """LC product line items"""
    __tablename__ = "lc_products"
    
    line_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    lc_id: Mapped[int] = mapped_column(Integer, ForeignKey('lc_master.lc_id', ondelete='CASCADE'), nullable=False, index=True)
    
    # Product details
    product_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    product_name: Mapped[str | None] = mapped_column(Text)          # LC goods description (45A) — can be several lines
    # Normalized short item code (closed set) + review flag when it couldn't be mapped.
    item_code: Mapped[str | None] = mapped_column(String(20), index=True)
    item_review: Mapped[bool | None] = mapped_column(Boolean, default=False)
    origin: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    quality: Mapped[str] = mapped_column(String(50), nullable=False)
    cargo_nature: Mapped[str | None] = mapped_column(String(50))

    # Quantity and pricing
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(20), default='MT')
    lc_unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    
    # LME comparison (computed columns)
    current_lme_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    # price_difference and price_diff_percent are computed
    
    # Additional info
    hs_code: Mapped[str | None] = mapped_column(String(50))
    grade: Mapped[str | None] = mapped_column(String(50))
    size: Mapped[str | None] = mapped_column(String(50))
    mill_name: Mapped[str | None] = mapped_column(String(200))
    lc_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    shipped_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), default=0)
    pkgs_coils: Mapped[int | None] = mapped_column(Integer, default=0)
    num_containers: Mapped[int | None] = mapped_column(Integer, default=0)

    # Tracking
    last_lme_update: Mapped[datetime | None] = mapped_column(DateTime)
    change_detected: Mapped[bool | None] = mapped_column(Boolean, default=False, index=True)
    
    baseline_lme: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    baseline_bulletin_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('shared.lme_bulletins.bulletin_id'))
    lme_date_from: Mapped[date | None] = mapped_column(Date)
    lme_date_to: Mapped[date | None] = mapped_column(Date)
    lc_lme_difference: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    current_lme: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    previous_lme: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    lme_change_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    lme_change_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    
    # Relationships
    lc = relationship("LCMaster", back_populates="products")
    alerts = relationship("PriceAlert", back_populates="product")

    __table_args__ = (
        CheckConstraint("quality IN ('PRIME', 'SECONDARY', 'PRM', 'SEC')", name='valid_quality'),
    )

    @property
    def price_difference(self):
        """Calculate price difference"""
        current_lme = cast(Decimal | None, self.current_lme_price)
        lc_price = cast(Decimal | None, self.lc_unit_price)
        if current_lme and lc_price:
            return float(current_lme) - float(lc_price)
        return None
    
    @property
    def price_diff_percent(self):
        """Calculate price difference percentage"""
        current_lme = cast(Decimal | None, self.current_lme_price)
        lc_price = cast(Decimal | None, self.lc_unit_price)
        if current_lme and lc_price and float(lc_price) > 0:
            return ((float(current_lme) - float(lc_price)) / float(lc_price)) * 100
        return None


class LCBuyerAllocation(Base):
    """Structured buyer / booked-by allocation for an LC (new — replaces the free-text
    booked_by for clean reporting). One row per buyer. For SINGLE-buyer LCs there is one
    row at 100%. Quantity/amount are the buyer's share of the LC total; share_percent is
    the % of the LC. The legacy lc_master.booked_by text is never touched by these rows."""
    __tablename__ = "lc_buyer_allocations"

    alloc_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    lc_id: Mapped[int] = mapped_column(Integer, ForeignKey('lc_master.lc_id', ondelete='CASCADE'),
                   nullable=False, index=True)
    # Clean buyer name (from the booked_by master). buyer_id is optional — kept for a future
    # hard FK; buyer_name is the denormalised value used by reports.
    buyer_id: Mapped[int | None] = mapped_column(Integer)
    buyer_name: Mapped[str] = mapped_column(String(300), nullable=False)
    share_percent: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))     # % of the LC (0-100)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))         # buyer's share of LC quantity (MT)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))           # buyer's share of LC amount (LC currency)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())

    lc = relationship("LCMaster", back_populates="buyer_allocations")




class Contract(Base):
    """Supplier Purchase Contract — the commercial origin of a deal, upstream of the LC.
    Modelled on the legacy ERP 'Supplier Purchase Contract' screen + the contract PDF.
    One contract : one LC (for now). Status DRAFT -> FINAL -> ACTUAL (set ACTUAL when an
    LC is saved against it). ERP master-data codes are intentionally not stored."""
    __tablename__ = "contracts"

    contract_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Source / document
    document_filename: Mapped[str | None] = mapped_column(String(255))
    document_path: Mapped[str | None] = mapped_column(Text)
    raw_extracted_data: Mapped[Any | None] = mapped_column(JSONB)
    source: Mapped[str | None] = mapped_column(String(20), default='UPLOADED')        # UPLOADED | MANUAL

    # Identity
    contract_number: Mapped[str | None] = mapped_column(String(50), index=True)       # "Supplier Contract Number"
    contract_date: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str | None] = mapped_column(String(20), default='DRAFT')           # DRAFT | FINAL | ACTUAL | CANCELLED

    # Parties (names as text — ERP codes dropped)
    supplier_name: Mapped[str | None] = mapped_column(String(300))                    # ERP "Supplier A/c. Name" (seller)
    buyer_name: Mapped[str | None] = mapped_column(String(300))                       # ERP "Importer Name"
    indentor_name: Mapped[str | None] = mapped_column(String(300))                    # ERP "Indentor Name" e.g. SELF / DIRECT

    # Bank + timeline tracking: which bank the contract was sent to, and WHEN it was sent.
    # Paired with the LC's import_date (when the bank returned the LC) to measure bank
    # turnaround time in reporting.
    bank_name: Mapped[str | None] = mapped_column(String(200))
    sent_to_bank_at: Mapped[datetime | None] = mapped_column(DateTime)                     # defaults to upload time

    # Terms (ERP header) — free text on real contracts can be long sentences
    payment_terms: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str | None] = mapped_column(String(10))
    delivery_terms: Mapped[str | None] = mapped_column(String(120))                   # CFR ... by Bulk
    country_of_origin: Mapped[str | None] = mapped_column(String(100))

    # Document-derived extras (on the contract PDF, not the ERP screen)
    buyer_ntn: Mapped[str | None] = mapped_column(String(20))
    buyer_address: Mapped[str | None] = mapped_column(Text)
    supplier_address: Mapped[str | None] = mapped_column(Text)
    quantity_tolerance: Mapped[str | None] = mapped_column(Text)                      # can be a full clause
    port_of_loading: Mapped[str | None] = mapped_column(String(200))
    shipping_mark: Mapped[str | None] = mapped_column(String(100))
    hs_code: Mapped[str | None] = mapped_column(String(50))
    beneficiary_bank: Mapped[str | None] = mapped_column(String(200))
    beneficiary_swift: Mapped[str | None] = mapped_column(String(20))
    beneficiary_account: Mapped[str | None] = mapped_column(String(50))
    beneficiary_bank_addr: Mapped[str | None] = mapped_column(Text)
    documents_required: Mapped[str | None] = mapped_column(Text)

    notes: Mapped[str | None] = mapped_column(Text)

    # Audit
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    updated_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))

    # Relationships
    items = relationship("ContractItem", back_populates="contract",
                         cascade="all, delete-orphan")
    lc = relationship("LCMaster", back_populates="contract",
                      foreign_keys="LCMaster.contract_id", uselist=False)
    creator = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        CheckConstraint("status IN ('DRAFT','FINAL','ACTUAL','CANCELLED')", name='valid_contract_status'),
    )


class ContractItem(Base):
    """A line on the contract's item grid (ERP multi-row). Keeps BOTH the L/C price/amount
    and the actual Purchase rate/amount, per the ERP screen."""
    __tablename__ = "contract_items"

    item_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    contract_id: Mapped[int] = mapped_column(Integer, ForeignKey('contracts.contract_id', ondelete='CASCADE'),
                         nullable=False, index=True)
    line_no: Mapped[int | None] = mapped_column(Integer)
    product_name: Mapped[str | None] = mapped_column(String(300))
    item_code: Mapped[str | None] = mapped_column(String(20), index=True)              # normalized short code (closed set)
    item_review: Mapped[bool | None] = mapped_column(Boolean, default=False)
    size_description: Mapped[str | None] = mapped_column(String(100))
    grade_description: Mapped[str | None] = mapped_column(String(100))                 # e.g. DX-55D
    mill_name: Mapped[str | None] = mapped_column(String(200))
    weight_mt: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    lc_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))                        # L/C price
    lc_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))                       # L/C amount
    purchase_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))                   # actual buy price (may be 0/unset)
    purchase_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))

    contract = relationship("Contract", back_populates="items")


class _LookupBase:
    """Shared columns for the name-lookup master tables (importers / suppliers /
    indentors / booked_by). Names only for now, with headroom for category-specific
    columns later. name_norm = uppercased/trimmed, used for dedup + search."""
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    name_norm: Mapped[str] = mapped_column(String(300), nullable=False, unique=True, index=True)
    # Set when a value could not be confidently matched to an existing clean master and
    # was auto-created — surfaces in review queues so a human can merge/rename it.
    needs_review: Mapped[bool | None] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[int | None] = mapped_column(Integer)


class Importer(_LookupBase, Base):
    __tablename__ = "importers"
    # Standard short code for reports/filters (PCL, RSI, MCL, …). Raw names on LCs/GDs stay as-is.
    short_code: Mapped[str | None] = mapped_column(String(12), index=True)


class Supplier(_LookupBase, Base):
    __tablename__ = "suppliers"


class Indentor(_LookupBase, Base):
    __tablename__ = "indentors"


class BookedBy(_LookupBase, Base):
    """The person who books the contract and then manages it (payments etc.).
    A names list — intentionally NOT the users table."""
    __tablename__ = "booked_by"


class Bank(_LookupBase, Base):
    """Issuing/handling bank master — clean bank names for contract/LC bank selection and
    normalised reporting."""
    __tablename__ = "banks"


class BankLimit(Base):
    """A bank's LC-amount limit allotted to our group, for a validity window. Revisions are
    NEW rows (never overwrite) — the report picks the revision(s) overlapping a date range.
    Amounts are PKR. Company-wise sub-limits live in BankLimitLine."""
    __tablename__ = "bank_limits"

    limit_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    bank_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)   # canonical (banks master)
    branch: Mapped[str | None] = mapped_column(String(200))                                   # free text (no branch master)
    group_company: Mapped[str] = mapped_column(String(300), nullable=False, index=True)  # parent, e.g. Perfect Craft
    # Nature of the limit: REGULAR (yearly/revolving), ONE_OFF (one-time special), or
    # TEMPORARY (time-boxed extension over the current limit).
    bank_limit_type: Mapped[str | None] = mapped_column(String(12), default='REGULAR')       # REGULAR | ONE_OFF | TEMPORARY
    # Which LC tenors this limit applies to.
    lc_type: Mapped[str | None] = mapped_column(String(10), default='BOTH')                  # SIGHT | DA | BOTH
    valid_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    valid_to: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    group_limit_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))                    # total/group limit (PKR)
    revision_no: Mapped[int | None] = mapped_column(Integer, default=1)                       # 1,2,3… within bank+group
    remarks: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_by: Mapped[int | None] = mapped_column(Integer)

    lines = relationship("BankLimitLine", back_populates="limit",
                         cascade="all, delete-orphan")


class BankLimitLine(Base):
    """Company-wise sub-limit line under one BankLimit. limit_type: PARENT (the group/parent
    company) or SUB (a child company)."""
    __tablename__ = "bank_limit_lines"

    line_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    limit_id: Mapped[int] = mapped_column(Integer, ForeignKey('bank_limits.limit_id', ondelete='CASCADE'),
                      nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(300), nullable=False)             # matched to importers master
    # PARENT = the group/parent (e.g. Perfect Craft, may use the full available group limit);
    # CHILD = an allowed portion of the group limit. 'SUB' is the legacy value for CHILD.
    limit_type: Mapped[str | None] = mapped_column(String(10), default='CHILD')              # PARENT / CHILD (legacy: SUB)
    lc_type: Mapped[str | None] = mapped_column(String(10), default='BOTH')                  # SIGHT | DA | BOTH
    sub_limit_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))                      # PKR

    limit = relationship("BankLimit", back_populates="lines")

    __table_args__ = (
        CheckConstraint("limit_type IN ('PARENT','SUB','CHILD')", name='valid_bank_limit_type'),
    )


class LMEBulletin(Base):
    """Uploaded FastMarkets PDFs (source='MANUAL') and auto-synced web bulletins (source='WEB')"""
    __tablename__ = "lme_bulletins"

    bulletin_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    bulletin_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    upload_date: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str | None] = mapped_column(Text)
    file_size: Mapped[int | None] = mapped_column(Integer)
    uploaded_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))
    extracted_data: Mapped[Any | None] = mapped_column(JSONB)
    rate_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('shared.currency_rates.rate_id'))
    symbols_extracted: Mapped[int | None] = mapped_column(Integer, default=0)
    prices_stored: Mapped[int | None] = mapped_column(Integer, default=0)
    # 'MANUAL' (default, existing upload flow) or 'WEB' (auto-synced from thehelpers.pk).
    # Unique per (date, source) rather than per date alone, so a web sync and a manual
    # upload for the same bulletin_date can coexist instead of one clobbering the other.
    source: Mapped[str] = mapped_column(String(10), nullable=False, server_default='MANUAL')

    # WhatsApp rates-report delivery tracking. Unlike PriceAlert/SystemAlert there's no
    # per-alert row to flag - this is the bulletin-level marker send_rates_report() uses
    # to know whether its PDF for this bulletin still needs (re)sending on the next
    # 15-minute retry pass. Only set True once every active recipient succeeded.
    whatsapp_rates_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default='false')
    whatsapp_rates_sent_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Relationships - REQUIRED for LMEPrice model
    prices = relationship("LMEPrice", back_populates="bulletin")

    __table_args__ = (
        UniqueConstraint('bulletin_date', 'source', name='uq_bulletin_date_source'),
        {"schema": SHARED_SCHEMA},
    )
class LMEPrice(Base):
    """Extracted and calculated LME prices"""
    __tablename__ = "lme_prices"
    
    price_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    bulletin_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('shared.lme_bulletins.bulletin_id', ondelete='CASCADE'))
    
    # Price source
    fastmarket_symbol: Mapped[str | None] = mapped_column(String(50))
    product_code: Mapped[str] = mapped_column(String(20), nullable=False)
    origin: Mapped[str] = mapped_column(String(50), nullable=False)
    quality: Mapped[str | None] = mapped_column(String(20))
    
    # Raw prices
    low_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    high_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    # average_price is computed
    
    # Calculated LME
    calculated_lme: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    formula_used: Mapped[str | None] = mapped_column(String(20))
    calculation_date: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), index=True)
    
    # EUR/USD conversion
    eur_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    usd_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    conversion_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    
    # Additional metadata
    notes: Mapped[str | None] = mapped_column(Text)
    
    # Relationships
    bulletin = relationship("LMEBulletin", back_populates="prices")
    
    __table_args__ = (
        CheckConstraint('low_price <= high_price', name='valid_prices'),
        Index('idx_lme_product', 'product_code', 'origin', 'quality'),
        {"schema": SHARED_SCHEMA},
    )
    
    @property
    def average_price(self):
        """Calculate average price"""
        low = cast(Decimal | None, self.low_price)
        high = cast(Decimal | None, self.high_price)
        if low and high:
            return (float(low) + float(high)) / 2
        return None

class PriceAlert(Base):
    """Price change alerts"""
    __tablename__ = "price_alerts"
    
    alert_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    lc_id: Mapped[int] = mapped_column(Integer, ForeignKey('lc_master.lc_id', ondelete='CASCADE'), nullable=False, index=True)
    line_id: Mapped[int] = mapped_column(Integer, ForeignKey('lc_products.line_id', ondelete='CASCADE'), nullable=False)
    
    # Alert details
    alert_date: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), index=True)
    alert_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    priority: Mapped[str | None] = mapped_column(String(20), default='MEDIUM')
    
    # Price information
    old_lme_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    new_lme_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    lc_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    difference: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    difference_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    
    # Potential impact
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # total_impact is computed
    
    # Alert delivery
    whatsapp_sent: Mapped[bool | None] = mapped_column(Boolean, default=False, index=True)
    whatsapp_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    whatsapp_delivery_status: Mapped[str | None] = mapped_column(String(50))
    whatsapp_message_id: Mapped[str | None] = mapped_column(String(100))
    
    # Dashboard tracking
    viewed: Mapped[bool | None] = mapped_column(Boolean, default=False, index=True)
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    viewed_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))
    
    # Action taken
    action_taken: Mapped[str | None] = mapped_column(String(50))
    action_date: Mapped[datetime | None] = mapped_column(DateTime)
    action_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'), index=True)
    action_notes: Mapped[str | None] = mapped_column(Text)
    
    # Relationships
    lc = relationship("LCMaster", back_populates="alerts")
    product = relationship("LCProduct", back_populates="alerts")
    viewer = relationship("User", foreign_keys=[viewed_by])
    actor = relationship("User", foreign_keys=[action_by])
    
    __table_args__ = (
        CheckConstraint("alert_type IN ('DECREASE', 'INCREASE')", name='valid_alert_type'),
        CheckConstraint("priority IN ('HIGH', 'MEDIUM', 'LOW')", name='valid_priority'),
    )
    
    @property
    def total_impact(self):
        """Calculate total impact"""
        diff = cast(Decimal | None, self.difference)
        qty = cast(Decimal | None, self.quantity)
        if diff and qty:
            return float(diff) * float(qty)
        return None

class WhatsAppConfig(Base):
    """WhatsApp configuration per user"""
    __tablename__ = "whatsapp_config"
    
    config_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id', ondelete='CASCADE'), index=True)
    recipient_name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    country_code: Mapped[str | None] = mapped_column(String(5), default='+92')
    
    # Alert preferences
    alert_type: Mapped[str | None] = mapped_column(String(50), default='ALL')
    min_amount_threshold: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    min_percent_threshold: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    
    # Status
    active: Mapped[bool | None] = mapped_column(Boolean, default=True)
    last_message_sent: Mapped[datetime | None] = mapped_column(DateTime)
    total_messages_sent: Mapped[int | None] = mapped_column(Integer, default=0)
    
    # Settings
    send_daily_summary: Mapped[bool | None] = mapped_column(Boolean, default=False)
    send_weekly_report: Mapped[bool | None] = mapped_column(Boolean, default=False)
    quiet_hours_start: Mapped[time | None] = mapped_column(Time)
    quiet_hours_end: Mapped[time | None] = mapped_column(Time)
    
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    
    # Relationships
    user = relationship("User", foreign_keys=[user_id])

class DemurrageConfig(Base):
    """Global defaults for demurrage calculation. Singleton row (config_id=1).
    Free days + per-day charge prefill the per-BL demurrage fields; warn_days controls
    how early the alert engine flags an approaching last free day."""
    __tablename__ = "demurrage_config"

    config_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    free_days: Mapped[int | None] = mapped_column(Integer, default=7)
    per_day_charge: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), default=0)
    currency: Mapped[str | None] = mapped_column(String(10), default='USD')
    warn_days: Mapped[int | None] = mapped_column(Integer, default=3)            # alert when last free day is within N days
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

class BrandingConfig(Base):
    """Global app branding — name, logo, and logo background color. Singleton row
    (config_id=1), same pattern as DemurrageConfig. Lazily created via
    modules.branding.service.get_or_create_config() on first request."""
    __tablename__ = "branding_config"

    config_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    app_name: Mapped[str] = mapped_column(String(120), nullable=False, default="JM Traders")
    logo_path: Mapped[str | None] = mapped_column(String(500))
    logo_original_filename: Mapped[str | None] = mapped_column(String(255))
    logo_bg_color: Mapped[str] = mapped_column(String(20), nullable=False, default="#0F172A")
    updated_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id', ondelete='SET NULL'))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

class AuditLog(Base):
    """Audit trail for all actions"""
    __tablename__ = "audit_log"
    
    log_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'), index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(50), index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    
    # Details
    old_value: Mapped[Any | None] = mapped_column(JSONB)
    new_value: Mapped[Any | None] = mapped_column(JSONB)
    description: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(INET)
    
    # Timestamp
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), index=True)
    
    # Relationships
    user = relationship("User")
    
    __table_args__ = (
        Index('idx_audit_entity', 'entity_type', 'entity_id'),
    )
class LMEPriceHistory(Base):
    """Historical LME prices from FastMarkets bulletins"""
    __tablename__ = 'lme_price_history'
    
    price_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    bulletin_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('shared.lme_bulletins.bulletin_id'))
    bulletin_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    
    # New Phase 2 columns (PDF upload)
    symbol: Mapped[str | None] = mapped_column(String(20), index=True)
    product_type: Mapped[str | None] = mapped_column(String(50))
    region: Mapped[str | None] = mapped_column(String(50), index=True)
    low_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    high_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    avg_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    
    # Old Phase 1 columns (keep for compatibility)
    product_code: Mapped[str | None] = mapped_column(String(10), index=True)
    origin: Mapped[str | None] = mapped_column(String(50), index=True)
    quality: Mapped[str | None] = mapped_column(String(20), index=True)
    lme_high: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    lme_low: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    calculated_lme: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    formula_number: Mapped[int | None] = mapped_column(Integer)  # legacy formula_id ref; tenant-scoped formulas
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = ({"schema": SHARED_SCHEMA},)


class LMESyncRun(Base):
    """Execution history for the thehelpers.pk auto-sync job (cron and manual runs)."""
    __tablename__ = "lme_sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default='RUNNING')
    trigger: Mapped[str] = mapped_column(String(10), nullable=False)  # 'CRON' or 'MANUAL'
    triggered_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))
    bulletin_date_found: Mapped[date | None] = mapped_column(Date)
    source_url: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("status IN ('RUNNING','SUCCESS','FAILED','SKIPPED_NO_NEW_DATA')", name='valid_sync_status'),
        CheckConstraint("trigger IN ('CRON','MANUAL')", name='valid_sync_trigger'),
        {"schema": SHARED_SCHEMA},
    )


class LMEBulletinCrawlLog(Base):
    """Execution history for the bulletin auto-crawler (thehelpers.pk -> manual upload
    pipeline, see integrations/thehelpers/thehelpers_bulletin_crawler.py). One row per
    bulletin candidate a run considered, not one row per scheduler tick - a run that
    finds 2 new bulletins in its lookback window writes 2 rows. Separate from
    LMESyncRun, which tracks the different (WEB-source) auto-sync job against the
    same site."""
    __tablename__ = "lme_bulletin_crawl_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default='RUNNING')
    trigger: Mapped[str] = mapped_column(String(10), nullable=False)  # 'CRON' or 'MANUAL'
    bulletin_date_found: Mapped[date | None] = mapped_column(Date)
    source_url: Mapped[str | None] = mapped_column(Text)
    file_name: Mapped[str | None] = mapped_column(String(255))
    bulletin_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey('shared.lme_bulletins.bulletin_id', ondelete='SET NULL'))
    error_message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('RUNNING','SUCCESS','FAILED','SKIPPED_ALREADY_IMPORTED')",
            name='valid_crawl_log_status'),
        CheckConstraint("trigger IN ('CRON','MANUAL')", name='valid_crawl_log_trigger'),
        {"schema": SHARED_SCHEMA},
    )


class BillOfLading(Base):
    """Bill of Lading documents linked to LCs"""
    __tablename__ = "bill_of_ladings"

    bl_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    lc_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('lc_master.lc_id', ondelete='SET NULL'), nullable=True, index=True)
    shipment_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('shipments.shipment_id', ondelete='SET NULL'), nullable=True, index=True)

    # Document storage
    document_filename: Mapped[str | None] = mapped_column(String(255))
    document_path: Mapped[str | None] = mapped_column(Text)
    raw_extracted_data: Mapped[Any | None] = mapped_column(JSONB)
    source: Mapped[str | None] = mapped_column(String(20), default='MANUAL')

    # BL Header
    bl_number: Mapped[str | None] = mapped_column(String(100), index=True)
    bl_date: Mapped[date | None] = mapped_column(Date)
    bl_issue_place: Mapped[str | None] = mapped_column(String(200))
    original_bl_count: Mapped[int | None] = mapped_column(Integer)
    # Manual override for when this BL was actually uploaded/received — falls back to
    # created_at (the real upload timestamp) when unset. See services.py response builder.
    upload_date: Mapped[date | None] = mapped_column(Date)

    # Parties
    shipper_name: Mapped[str | None] = mapped_column(String(300))
    shipper_address: Mapped[str | None] = mapped_column(Text)
    consignee: Mapped[str | None] = mapped_column(Text)
    notify_party: Mapped[str | None] = mapped_column(Text)
    carrier_name: Mapped[str | None] = mapped_column(String(200))
    shipping_agent: Mapped[str | None] = mapped_column(String(300))

    # Vessel / Transport
    vessel_name: Mapped[str | None] = mapped_column(String(200))
    voyage_number: Mapped[str | None] = mapped_column(String(50))
    pre_carriage_by: Mapped[str | None] = mapped_column(String(200))
    place_of_receipt: Mapped[str | None] = mapped_column(String(200))

    # Ports
    port_of_loading: Mapped[str | None] = mapped_column(String(200))
    port_of_discharge: Mapped[str | None] = mapped_column(String(200))
    final_destination: Mapped[str | None] = mapped_column(String(200))
    freight_payable_at: Mapped[str | None] = mapped_column(String(200))

    # Cargo
    shipping_marks: Mapped[str | None] = mapped_column(String(100))
    package_count: Mapped[int | None] = mapped_column(Integer)
    package_type: Mapped[str | None] = mapped_column(String(50))
    goods_description: Mapped[str | None] = mapped_column(Text)
    # Shipment type detected during extraction (AI signal, falls back to a regex
    # heuristic — see resolve_bl_type() in container_detention_service.py). Persisted
    # so downstream logic (alerts, reports, detention) reads one authoritative value
    # instead of re-guessing on every request.
    bl_type: Mapped[str | None] = mapped_column(String(20))
    gross_weight_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    net_weight_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    measurement_m3: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    applicant_ntn: Mapped[str | None] = mapped_column(String(50))

    # Terms
    freight_terms: Mapped[str | None] = mapped_column(String(50))
    shipped_on_board_clause: Mapped[str | None] = mapped_column(String(100))

    # Demurrage tracking
    demurrage_start_date: Mapped[date | None] = mapped_column(Date)               # manual; clock start (cargo arrival / free-time start)
    free_days: Mapped[int | None] = mapped_column(Integer)                       # prefilled from DemurrageConfig, editable
    demurrage_total_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))   # total demurrage actually paid
    demurrage_currency: Mapped[str | None] = mapped_column(String(10), default='USD')
    demurrage_cleared_date: Mapped[date | None] = mapped_column(Date)             # manual; null = still accruing (clock stop)

    # Detention tracking (container held past free time — separate clock from demurrage)
    detention_start_date: Mapped[date | None] = mapped_column(Date)
    detention_free_days: Mapped[int | None] = mapped_column(Integer)
    detention_end_date: Mapped[date | None] = mapped_column(Date)                 # container returned / detention period ended
    detention_total_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))   # total detention actually paid
    detention_currency: Mapped[str | None] = mapped_column(String(10), default='USD')
    detention_paid_date: Mapped[date | None] = mapped_column(Date)
    detention_remarks: Mapped[str | None] = mapped_column(Text)

    # Status
    status: Mapped[str | None] = mapped_column(String(20), default='PENDING_REVIEW', index=True)
    notes: Mapped[str | None] = mapped_column(Text)

    # Audit
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    updated_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))

    # Relationships
    lc = relationship("LCMaster", back_populates="bill_of_ladings")
    shipment = relationship("Shipment", back_populates="bill_of_ladings")
    creator = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        CheckConstraint(
            "source IN ('UPLOADED', 'IMPORTED', 'MANUAL')",
            name='valid_bl_source'
        ),
        CheckConstraint(
            "status IN ('IMPORTED', 'PENDING_REVIEW', 'VERIFIED', 'RELEASED')",
            name='valid_bl_status'
        ),
        CheckConstraint(
            "bl_type IS NULL OR bl_type IN ('COIL', 'CONTAINER')",
            name='valid_bl_type'
        ),
        # The alert engine's demurrage scan only cares about BLs still accruing
        # (cleared_date IS NULL) — a partial index keeps it scoped to just those rows.
        Index('idx_bl_demurrage_open', 'demurrage_start_date', 'demurrage_cleared_date',
              postgresql_where=text('demurrage_cleared_date IS NULL')),
    )


class Shipment(Base):
    """Central shipment entity — groups BL + Invoice + Packing + GD under an LC.
    One LC has many shipments (partial shipments)."""
    __tablename__ = "shipments"

    shipment_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    lc_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('lc_master.lc_id', ondelete='SET NULL'), nullable=True, index=True)
    shipment_ref: Mapped[str | None] = mapped_column(String(100))               # internal reference / auto
    category: Mapped[str | None] = mapped_column(String(20))                     # FIRST / SECOND / THIRD ... (partial shipment label)
    lot_number: Mapped[str | None] = mapped_column(String(50))                   # import team's lot reference for this shipment

    status: Mapped[str | None] = mapped_column(String(30), default='PENDING', index=True)
    # Automatic status flow (derived — see services.shipment_status):
    #   PENDING -> COPY_DOCS_RECEIVED -> DOCS_AT_BANK -> PAYMENT_RECEIVED
    #           -> ORIGINAL_DOCS_RECEIVED -> DELIVERED
    # Legacy codes (DOCUMENTS_RECEIVED/VALIDATED/DISCREPANT/BANK_PRESENTATION/ACCEPTED/
    #   PAYMENT_MADE/CLOSED) remain valid for un-backfilled rows.

    # Transport (denormalised from BL for quick access)
    vessel_name: Mapped[str | None] = mapped_column(String(200))
    voyage_number: Mapped[str | None] = mapped_column(String(50))
    port_of_loading: Mapped[str | None] = mapped_column(String(200))
    port_of_discharge: Mapped[str | None] = mapped_column(String(200))
    bl_date: Mapped[date | None] = mapped_column(Date)
    etd: Mapped[date | None] = mapped_column(Date)                                 # estimated time of departure (defaults from BL/shipped-on-board date)
    eta: Mapped[date | None] = mapped_column(Date, index=True)
    # Per-shipment override for the auto-ETA formula (etd + transit_days business days).
    # None => use the system default (see modules.shipments.eta_calc.DEFAULT_TRANSIT_DAYS).
    transit_days: Mapped[int | None] = mapped_column(Integer)
    # Who last set `eta`: 'AUTO' (formula: etd + transit_days), 'WEBSITE' (KPT tracker — real
    # port-authority data, see integrations/kpt/kpt_eta_sync.py), 'MANUAL' (typed on the
    # shipment edit form or the vessel bulk-update form). The auto-formula only recalculates
    # while this is 'AUTO' (or unset) — once a human or the KPT tracker asserts a real ETA,
    # the formula backs off until explicitly reset (see apply_shipment_fields()).
    eta_source: Mapped[str | None] = mapped_column(String(10))
    on_port_date: Mapped[date | None] = mapped_column(Date)                        # actual on-port date (KPT or manual)
    departure_date: Mapped[date | None] = mapped_column(Date)                      # actual departure date (KPT or manual)
    kpt_berth: Mapped[str | None] = mapped_column(String(100))                    # berth from KPT on-port / departures crawl
    delivery_date: Mapped[date | None] = mapped_column(Date)                       # set => shipment delivered/closed (drives DELIVERED status)
    vessel_location: Mapped[str | None] = mapped_column(String(200))             # manual free-text (e.g. "Singapore", "India")
    # Who last wrote vessel_location/status: 'WEBSITE' (KPT scraper) or 'MANUAL' (edited on
    # the shipment/vessel-report form). Written by both apply_shipment_fields() (services.py)
    # and apply_arrival_to_shipments() (integrations/kpt/kpt_eta_sync.py) — surfaced in the
    # UI as a small source badge next to vessel status.
    vessel_status_source: Mapped[str | None] = mapped_column(String(10))
    vessel_status_updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    # KPT crawler milestones — drive document/pickup alert schedules
    kpt_eta_at: Mapped[datetime | None] = mapped_column(DateTime)
    kpt_on_port_at: Mapped[datetime | None] = mapped_column(DateTime)
    kpt_departed_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Manual finance/document milestone dates (entered on the shipment edit form)
    payment_date: Mapped[date | None] = mapped_column(Date)
    original_doc_date: Mapped[date | None] = mapped_column(Date)                   # date original documents received
    retirement_date: Mapped[date | None] = mapped_column(Date)                     # set => LC retired -> shipment CLOSED
    intimation_date: Mapped[date | None] = mapped_column(Date)
    maturity_date: Mapped[date | None] = mapped_column(Date)                        # DA LCs only (usance maturity)
    exchange_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))             # manual FX rate booked for this shipment

    # Reconciled totals (from documents)
    total_coils: Mapped[int | None] = mapped_column(Integer)
    total_net_weight_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    total_gross_weight_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))

    # Quantity variance (LC-level short-shipment tracking)
    expected_quantity_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))     # LC remaining balance suggested at creation
    delivered_quantity_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))    # actual from invoice/packing
    quantity_variance_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))     # delivered - expected (negative = short)

    # Validation
    validation_status: Mapped[str | None] = mapped_column(String(20), default='PENDING')   # PENDING / ALL_CLEAR / DISCREPANT
    validation_notes: Mapped[str | None] = mapped_column(Text)

    # Manual remarks (free text, entered on the shipment edit form)
    remarks: Mapped[str | None] = mapped_column(Text)

    # Soft delete — deleted shipments are hidden from lists/reports/balances but kept on disk.
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))

    # Audit
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    updated_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))

    # Relationships
    lc = relationship("LCMaster", back_populates="shipments")
    bill_of_ladings = relationship("BillOfLading", back_populates="shipment")
    commercial_invoices = relationship("CommercialInvoice", back_populates="shipment", cascade="all, delete-orphan")
    packing_lists = relationship("PackingList", back_populates="shipment", cascade="all, delete-orphan")
    goods_declarations = relationship("GoodsDeclaration", back_populates="shipment", cascade="all, delete-orphan")
    financial_instruments = relationship("FinancialInstrument", back_populates="shipment", cascade="all, delete-orphan")
    insurance_certificates = relationship("InsuranceCertificate", back_populates="shipment", cascade="all, delete-orphan")
    validations = relationship("DocumentValidation", back_populates="shipment", cascade="all, delete-orphan")
    extra_documents = relationship("ShipmentDocument", back_populates="shipment", cascade="all, delete-orphan")
    creator = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','COPY_DOCS_RECEIVED','DOCS_AT_BANK','PAYMENT_RECEIVED',"
            "'ORIGINAL_DOCS_RECEIVED','DELIVERED',"
            # legacy codes kept valid for un-backfilled rows
            "'DOCUMENTS_RECEIVED','VALIDATED','DISCREPANT','BANK_PRESENTATION',"
            "'ACCEPTED','PAYMENT_MADE','CLOSED')",
            name='valid_shipment_status'
        ),
        CheckConstraint(
            "vessel_status_source IS NULL OR vessel_status_source IN ('WEBSITE', 'MANUAL')",
            name='valid_vessel_status_source'
        ),
        # Dashboard/reports/alert-engine all filter "active shipments by ETA" — a partial
        # index scoped to non-deleted rows keeps it small and avoids indexing rows no query needs.
        Index('idx_shipment_active_eta', 'eta', postgresql_where=text('is_deleted = false')),
        # vessel_name/shipment_ref are searched via leading-wildcard ilike('%term%') in the
        # shipments list, main report, and shipment-wise report — see the matching note on
        # LCMaster above; same fix, same reason.
        Index('idx_shipment_vessel_trgm', 'vessel_name',
              postgresql_using='gin', postgresql_ops={'vessel_name': 'gin_trgm_ops'}),
        Index('idx_shipment_ref_trgm', 'shipment_ref',
              postgresql_using='gin', postgresql_ops={'shipment_ref': 'gin_trgm_ops'}),
    )


class CommercialInvoice(Base):
    """Commercial Invoice — seller's price/quantity confirmation. Has line items."""
    __tablename__ = "commercial_invoices"

    invoice_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    shipment_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('shipments.shipment_id', ondelete='SET NULL'), nullable=True, index=True)
    lc_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('lc_master.lc_id', ondelete='SET NULL'), nullable=True, index=True)

    # Document
    document_filename: Mapped[str | None] = mapped_column(String(255))
    document_path: Mapped[str | None] = mapped_column(Text)
    raw_extracted_data: Mapped[Any | None] = mapped_column(JSONB)
    source: Mapped[str | None] = mapped_column(String(20), default='UPLOADED')

    # Header
    invoice_number: Mapped[str | None] = mapped_column(String(100), index=True)
    invoice_date: Mapped[date | None] = mapped_column(Date)
    documentary_credit_number: Mapped[str | None] = mapped_column(String(100), index=True)   # maps to LC
    # Manual override for when this invoice was actually uploaded/received — falls back to
    # created_at (the real upload timestamp) when unset.
    upload_date: Mapped[date | None] = mapped_column(Date)

    # Parties
    seller_name: Mapped[str | None] = mapped_column(String(300))
    seller_address: Mapped[str | None] = mapped_column(Text)
    buyer_name: Mapped[str | None] = mapped_column(String(300))
    buyer_address: Mapped[str | None] = mapped_column(Text)

    # Cargo
    goods_description: Mapped[str | None] = mapped_column(Text)
    grade: Mapped[str | None] = mapped_column(String(100))
    hs_code: Mapped[str | None] = mapped_column(String(50))   # may carry multiple codes, e.g. "7210.4910, 7210.7010"
    country_of_origin: Mapped[str | None] = mapped_column(String(100))

    # Financials (unit_price = RECORD only, decoupled from LME calc per Q4)
    incoterms: Mapped[str | None] = mapped_column(String(50))
    unit_price_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    total_net_weight_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    total_gross_weight_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    total_coils: Mapped[int | None] = mapped_column(Integer)
    total_amount_usd: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    currency: Mapped[str | None] = mapped_column(String(10), default='USD')

    # Transport
    vessel_name: Mapped[str | None] = mapped_column(String(200))
    voyage_number: Mapped[str | None] = mapped_column(String(50))
    port_of_loading: Mapped[str | None] = mapped_column(String(200))
    port_of_discharge: Mapped[str | None] = mapped_column(String(200))

    # Status
    status: Mapped[str | None] = mapped_column(String(20), default='PENDING_REVIEW', index=True)   # PENDING_REVIEW / VERIFIED

    # Audit
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    updated_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))

    # Relationships
    shipment = relationship("Shipment", back_populates="commercial_invoices")
    line_items = relationship("InvoiceLineItem", back_populates="invoice", cascade="all, delete-orphan")
    creator = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        CheckConstraint("status IN ('PENDING_REVIEW','VERIFIED')", name='valid_invoice_status'),
    )


class InvoiceLineItem(Base):
    """Invoice line item — per-size breakdown (thickness x width, qty, coils, price)."""
    __tablename__ = "invoice_line_items"

    line_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    invoice_id: Mapped[int] = mapped_column(Integer, ForeignKey('commercial_invoices.invoice_id', ondelete='CASCADE'), nullable=False, index=True)
    shipment_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('shipments.shipment_id', ondelete='SET NULL'), nullable=True, index=True)

    item_number: Mapped[int | None] = mapped_column(Integer)
    size_thickness_mm: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    size_width_mm: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))
    quantity_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    net_weight_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    gross_weight_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    number_of_coils: Mapped[int | None] = mapped_column(Integer)
    unit_price_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    line_amount_usd: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))

    invoice = relationship("CommercialInvoice", back_populates="line_items")


class PackingList(Base):
    """Packing List — size/coil/weight breakdown. Has line items."""
    __tablename__ = "packing_lists"

    packing_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    shipment_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('shipments.shipment_id', ondelete='SET NULL'), nullable=True, index=True)
    invoice_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('commercial_invoices.invoice_id', ondelete='SET NULL'), nullable=True, index=True)

    # Document
    document_filename: Mapped[str | None] = mapped_column(String(255))
    document_path: Mapped[str | None] = mapped_column(Text)
    raw_extracted_data: Mapped[Any | None] = mapped_column(JSONB)
    source: Mapped[str | None] = mapped_column(String(20), default='UPLOADED')

    # Header
    packing_number: Mapped[str | None] = mapped_column(String(100), index=True)
    packing_date: Mapped[date | None] = mapped_column(Date)
    goods_description: Mapped[str | None] = mapped_column(Text)
    hs_code: Mapped[str | None] = mapped_column(String(50))
    # Manual override for when this packing list was actually uploaded/received — falls back
    # to created_at (the real upload timestamp) when unset.
    upload_date: Mapped[date | None] = mapped_column(Date)

    # Totals
    total_net_weight_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    total_gross_weight_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    total_coils: Mapped[int | None] = mapped_column(Integer)

    # Status
    status: Mapped[str | None] = mapped_column(String(20), default='PENDING_REVIEW', index=True)

    # Audit
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    updated_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))

    # Relationships
    shipment = relationship("Shipment", back_populates="packing_lists")
    line_items = relationship("PackingLineItem", back_populates="packing", cascade="all, delete-orphan")
    creator = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        CheckConstraint("status IN ('PENDING_REVIEW','VERIFIED')", name='valid_packing_status'),
    )


class PackingLineItem(Base):
    """Packing list line item — per-size breakdown (no prices)."""
    __tablename__ = "packing_line_items"

    line_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    packing_id: Mapped[int] = mapped_column(Integer, ForeignKey('packing_lists.packing_id', ondelete='CASCADE'), nullable=False, index=True)
    shipment_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('shipments.shipment_id', ondelete='SET NULL'), nullable=True, index=True)

    item_number: Mapped[int | None] = mapped_column(Integer)
    grade: Mapped[str | None] = mapped_column(String(50))                        # steel grade, e.g. SWRH82B-B (not all docs carry it)
    size: Mapped[str | None] = mapped_column(String(50))                          # verbatim size text, e.g. "1.5 x 1220" or "8.0 MM"
    size_thickness_mm: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))          # parsed thickness when size is "thickness x width"
    size_width_mm: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))              # parsed width when size is "thickness x width"
    quantity_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    net_weight_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    gross_weight_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    number_of_coils: Mapped[int | None] = mapped_column(Integer)

    packing = relationship("PackingList", back_populates="line_items")


class DocumentValidation(Base):
    """Cross-document validation check result for a shipment."""
    __tablename__ = "document_validations"

    validation_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    shipment_id: Mapped[int] = mapped_column(Integer, ForeignKey('shipments.shipment_id', ondelete='CASCADE'), nullable=False, index=True)

    check_name: Mapped[str | None] = mapped_column(String(100))          # 'Total Coils Match', 'Net Weight Match', ...
    check_type: Mapped[str | None] = mapped_column(String(30))           # WEIGHT / COUNT / PRICE / DATE / DESCRIPTION / VARIANCE
    bl_value: Mapped[str | None] = mapped_column(String(200))
    invoice_value: Mapped[str | None] = mapped_column(String(200))
    packing_value: Mapped[str | None] = mapped_column(String(200))
    lc_value: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str | None] = mapped_column(String(20))               # PASS / FAIL / WARNING / SKIPPED
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())

    shipment = relationship("Shipment", back_populates="validations")

    __table_args__ = (
        CheckConstraint("status IN ('PASS','FAIL','WARNING','SKIPPED')", name='valid_validation_status'),
    )


class GoodsDeclaration(Base):
    """Goods Declaration (GD-I) filed with Pakistan Customs WEBOC. Record-only for now."""
    __tablename__ = "goods_declarations"

    gd_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    shipment_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('shipments.shipment_id', ondelete='SET NULL'), nullable=True, index=True)
    lc_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('lc_master.lc_id', ondelete='SET NULL'), nullable=True, index=True)
    invoice_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('commercial_invoices.invoice_id', ondelete='SET NULL'), nullable=True)
    bl_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('bill_of_ladings.bl_id', ondelete='SET NULL'), nullable=True)

    # Document
    document_filename: Mapped[str | None] = mapped_column(String(255))
    document_path: Mapped[str | None] = mapped_column(Text)
    raw_extracted_data: Mapped[Any | None] = mapped_column(JSONB)
    source: Mapped[str | None] = mapped_column(String(20), default='UPLOADED')

    # GD Identity
    gd_number: Mapped[str | None] = mapped_column(String(50), index=True)        # C-AI-MB-000677
    machine_number: Mapped[str | None] = mapped_column(String(50))               # AJ WB-IE-14358
    filing_date: Mapped[date | None] = mapped_column(Date, index=True)
    assessment_date: Mapped[date | None] = mapped_column(Date)
    custom_office: Mapped[str | None] = mapped_column(String(100))
    bank_code: Mapped[str | None] = mapped_column(String(20))
    girm_egm_number: Mapped[str | None] = mapped_column(String(100))

    # Financial Instrument (LC reference)
    financial_instrument_no: Mapped[str | None] = mapped_column(String(100))
    financial_instrument_date: Mapped[date | None] = mapped_column(Date)
    financial_instrument_value: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))

    # Parties
    importer_ntn: Mapped[str | None] = mapped_column(String(20))
    importer_name: Mapped[str | None] = mapped_column(String(300))
    exporter_name: Mapped[str | None] = mapped_column(String(300))

    # Transport
    vessel_name: Mapped[str | None] = mapped_column(String(200))
    bl_number: Mapped[str | None] = mapped_column(String(100))
    bl_date: Mapped[date | None] = mapped_column(Date)
    port_of_shipment: Mapped[str | None] = mapped_column(String(200))
    port_of_discharge: Mapped[str | None] = mapped_column(String(200))
    payment_terms: Mapped[str | None] = mapped_column(String(100))

    # Cargo
    hs_code: Mapped[str | None] = mapped_column(String(50))
    goods_description: Mapped[str | None] = mapped_column(Text)
    package_count: Mapped[int | None] = mapped_column(Integer)
    package_type: Mapped[str | None] = mapped_column(String(50))
    gross_weight_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    net_weight_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    country_of_origin: Mapped[str | None] = mapped_column(String(100))

    # Valuation (declared vs assessed by customs)
    declared_unit_value_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    assessed_unit_value_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    declared_value_usd: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    assessed_value_usd: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    declared_value_pkr: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    assessed_value_pkr: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    exchange_rate_pkr: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    freight_value_usd: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    insurance_value_usd: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    landing_charges_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))

    # Duty breakdown (PKR) — the 9 levies from old GD screen
    customs_duty_pkr: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    sales_tax_pkr: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    income_tax_pkr: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    additional_customs_duty_pkr: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    additional_sales_tax_pkr: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    regulatory_duty_pkr: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    igm_deblocking_pkr: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    extra_pkr: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    invoice_not_found_penalty_pkr: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    total_duties_pkr: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))

    # GD-level levy breakdown (footer "Revenue Recovery", incl. codes not in the per-item
    # table such as WSc) — list of {code, rate, amount_pkr}. Quick-display columns above stay.
    levy_breakdown: Mapped[Any | None] = mapped_column(JSONB)

    # LME prediction (nullable — future Option A phase)
    lme_predicted_unit_value_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    lme_predicted_assessed_value_usd: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    lme_predicted_total_duties_pkr: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))

    # GD type — drives the workflow end-state.
    # Into-bond ends at CLEARED once Ex-Bond liftings settle gross weight (1–2% tol).
    # UNKNOWN = declaration type could not be read; the UI flags it for review.
    gd_type: Mapped[str | None] = mapped_column(String(20), default='HOME_CONSUMPTION')  # HOME_CONSUMPTION / INTO_BOND / EX_BOND / UNKNOWN
    declaration_type_raw: Mapped[str | None] = mapped_column(String(30))   # exactly as printed on the GD View
    # Ex-bond GD can link back to the into-bond GD it draws from.
    into_bond_gd_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('goods_declarations.gd_id', ondelete='SET NULL'), nullable=True)
    # INTO-BOND only: bonded Gross Weight target; lifted later via ex-bond entries
    # until remaining is within 1–2% tolerance (then GD can CLEARED).
    bonded_qty_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))

    # INTO-BOND only: penalty for breaching the 180-day settle deadline. Payable when the
    # bond is still open past the deadline, or when the settling Ex-Bond is dated after it.
    # The amount is never computed — it comes off the document or from the user. A penalty
    # recorded on an assumed breach is disregarded once a back-dated Ex-Bond proves the
    # bond actually settled in time (see weboc_service.bond_summary -> penalty_stale).
    bond_penalty_pkr: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    bond_penalty_source: Mapped[str | None] = mapped_column(String(20))    # DOCUMENT / MANUAL
    bond_penalty_reason: Mapped[str | None] = mapped_column(Text)
    bond_penalty_days: Mapped[int | None] = mapped_column(Integer)         # days late the penalty was assessed for
    bond_penalty_recorded_at: Mapped[datetime | None] = mapped_column(DateTime)
    bond_penalty_recorded_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id', ondelete='SET NULL'))

    # GD View (WeBOC) — filed-stage document.
    # GD filing deadline = ETA + 18 days; into-bond settle deadline = IB filing date + 180 days.
    eta: Mapped[date | None] = mapped_column(Date)
    gd_view_uploaded: Mapped[bool | None] = mapped_column(Boolean, default=False)
    # Verification columns
    is_verified: Mapped[bool | None] = mapped_column(Boolean, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id', ondelete='SET NULL'), nullable=True)
    # Into-Bond: the filed IB GD document (step 2 after GD View) — starts the settle clock.
    into_bond_gd_uploaded: Mapped[bool | None] = mapped_column(Boolean, default=False)
    item_details_uploaded: Mapped[bool | None] = mapped_column(Boolean, default=False)
    # Section 82 / Penalty-F in the charges block => the GD was filed late.
    section82_penalty_pkr: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    late_filed: Mapped[bool | None] = mapped_column(Boolean, default=False)
    charges_breakdown: Mapped[Any | None] = mapped_column(JSONB)          # [{code, description, amount_pkr}]

    # Examination stage (manual entry — no AI). Optional to advance past EXAMINATION.
    examination_report: Mapped[str | None] = mapped_column(Text)
    vir_no: Mapped[str | None] = mapped_column(String(100))
    index_no: Mapped[str | None] = mapped_column(String(100))
    examined_date: Mapped[date | None] = mapped_column(Date)

    # Assessment stage (manual entry — no AI). Text note + image attachments (kind ASSESSMENT).
    assessment_report: Mapped[str | None] = mapped_column(Text)

    # Lab report — an availability flag (NOT a status). When true, the form is shown.
    lab_report_available: Mapped[bool | None] = mapped_column(Boolean, default=False)
    lab_officer: Mapped[str | None] = mapped_column(String(200))
    lab_report: Mapped[str | None] = mapped_column(Text)
    lab_report_assessment: Mapped[bool | None] = mapped_column(Boolean, default=False)

    # Status
    status: Mapped[str | None] = mapped_column(String(20), default='FILED')      # see CheckConstraint below
    # True once the Final GD has been uploaded + AI-extracted (drives the Released stage).
    final_gd_uploaded: Mapped[bool | None] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)

    # Audit
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    shipment = relationship("Shipment", back_populates="goods_declarations")
    creator = relationship("User", foreign_keys=[created_by])
    into_bond_gd = relationship("GoodsDeclaration", remote_side=[gd_id])
    attachments = relationship("GDAttachment", back_populates="gd",
                               cascade="all, delete-orphan")
    quota_items = relationship("SroQuotaItem", back_populates="gd",
                               cascade="all, delete-orphan")
    items = relationship("GDItem", back_populates="gd",
                         cascade="all, delete-orphan")
    # KGTL vs own weighbridge reconciliation, filled once the GD is closed.
    kgtl_weighments = relationship("GdKgtlWeighment", back_populates="gd",
                                   cascade="all, delete-orphan")
    # Ex-bond liftings recorded against THIS into-bond GD.
    ex_bond_entries = relationship(
        "ExBondEntry", back_populates="into_bond_gd", cascade="all, delete-orphan",
        foreign_keys="ExBondEntry.into_bond_gd_id")

    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT','FILED','EXAMINATION','ASSESSED',"
            "'RELEASED','INTO_BOND','CLEARED','DISPUTED')",
            name='valid_gd_status'),
        CheckConstraint(
            "gd_type IN ('HOME_CONSUMPTION','INTO_BOND','EX_BOND','UNKNOWN')",
            name='valid_gd_type'),
        CheckConstraint(
            "bond_penalty_source IS NULL OR bond_penalty_source IN ('DOCUMENT','MANUAL')",
            name='valid_bond_penalty_source'),
    )


class GDAttachment(Base):
    """Image/file attachments for a GD — used by the Examination Report and Lab Report
    (each can carry MULTIPLE images, unlike the single document_path on the GD itself)."""
    __tablename__ = "gd_attachments"

    attachment_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    gd_id: Mapped[int] = mapped_column(Integer, ForeignKey('goods_declarations.gd_id', ondelete='CASCADE'),
                   nullable=False, index=True)
    # Set only for Partial-GD-scoped documents (EX_BOND_GD_VIEW / EX_BOND_ITEM_DETAILS) —
    # scopes the attachment to one ex_bond_entries row instead of the whole GD. NULL for
    # every other kind (unaffected / backward compatible).
    ex_bond_entry_id: Mapped[int | None] = mapped_column(Integer, ForeignKey(
        'ex_bond_entries.entry_id', ondelete='CASCADE', use_alter=True,
        name='fk_gd_attachments_ex_bond_entry_id',
    ), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)        # EXAMINATION / LAB / ASSESSMENT / GD_VIEW / ITEM_DETAILS / FINAL_GD / INTO_BOND_GD / EX_BOND_GD / EX_BOND_GD_VIEW / EX_BOND_ITEM_DETAILS
    filename: Mapped[str | None] = mapped_column(String(255))
    file_path: Mapped[str | None] = mapped_column(Text)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    uploaded_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))

    gd = relationship("GoodsDeclaration", back_populates="attachments")

    __table_args__ = (
        CheckConstraint(
            "kind IN ('EXAMINATION','LAB','ASSESSMENT','GD_VIEW','ITEM_DETAILS',"
            "'FINAL_GD','INTO_BOND_GD','EX_BOND_GD','EX_BOND_GD_VIEW','EX_BOND_ITEM_DETAILS')",
            name='valid_gd_attachment_kind'),
    )


class GDItem(Base):
    """A single item line on a GD (fields 37-48): description, HS, quantity, the
    declared/assessed valuation, and that item's levy breakdown (CD/ST/RD/AST/IT...).
    A GD can have several items; this detail feeds the into-bond / ex-bond workflow."""
    __tablename__ = "gd_items"

    item_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    gd_id: Mapped[int] = mapped_column(Integer, ForeignKey('goods_declarations.gd_id', ondelete='CASCADE'),
                   nullable=False, index=True)
    item_number: Mapped[int | None] = mapped_column(Integer)                     # field 37

    hs_code: Mapped[str | None] = mapped_column(String(50))                       # field 41
    goods_description: Mapped[str | None] = mapped_column(Text)                   # field 42
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(15, 3))                  # field 38 (in unit_type)
    unit_type: Mapped[str | None] = mapped_column(String(20))                     # field 38(a) e.g. KG
    country_of_origin: Mapped[str | None] = mapped_column(String(100))            # field 39 CO code
    sro_no: Mapped[str | None] = mapped_column(Text)                              # field 40 (may be several refs)

    # Valuation — field 43 unit value, 44 total value, 45 custom value (declared + assessed)
    unit_value_declared: Mapped[Decimal | None] = mapped_column(Numeric(15, 4))
    unit_value_assessed: Mapped[Decimal | None] = mapped_column(Numeric(15, 4))
    total_value_declared_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    total_value_assessed_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    custom_value_declared_pkr: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    custom_value_assessed_pkr: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))

    # Levy/rate/sum-payable breakdown for THIS item (fields 46-48)
    levies: Mapped[Any | None] = mapped_column(JSONB)                             # [{code, rate, amount_pkr}]

    # Item Details (WeBOC) — declared vs assessed quantity and the SRO/EDB quota reference
    # this item draws against. Feeds SRO quota usage.
    declared_quantity: Mapped[Decimal | None] = mapped_column(Numeric(15, 3))
    assessed_quantity: Mapped[Decimal | None] = mapped_column(Numeric(15, 3))
    unit: Mapped[str | None] = mapped_column(String(20))
    quota_reference: Mapped[str | None] = mapped_column(Text)
    m_size: Mapped[str | None] = mapped_column(String(100))

    gd = relationship("GoodsDeclaration", back_populates="items")


class EdbApproval(Base):
    """An SRO / EDB quota entry. The Main SRO Number is the quota heading; the child
    Group SRO Numbers (sro_group_numbers) sit under it. Grants approved_qty_mt against
    an HS code for one company, valid start_date..end_date. GDs consume from it via
    SroQuotaItem / matched GD items; balance = approved_qty_mt - sum(consumed)."""
    __tablename__ = "edb_approvals"

    approval_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    approval_no: Mapped[str | None] = mapped_column(String(50), index=True)     # legacy EDB approval ref (e.g. V-319)
    company_name: Mapped[str | None] = mapped_column(String(300))
    main_sro_no: Mapped[str | None] = mapped_column(String(50), index=True)     # main quota/SRO heading, e.g. 7210.4910
    hs_code: Mapped[str | None] = mapped_column(String(20))
    part_name: Mapped[str | None] = mapped_column(String(300))
    use_type: Mapped[str | None] = mapped_column(String(50))                    # e.g. Industrial Use
    approved_qty_mt: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))         # total approved qty (can be raised later)
    unit: Mapped[str | None] = mapped_column(String(10), default='MT')
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    short_description: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    quota_items = relationship("SroQuotaItem", back_populates="approval")
    group_numbers = relationship("SroGroupNumber", back_populates="approval",
                                 cascade="all, delete-orphan")


class SroGroupNumber(Base):
    """A Group / child SRO number under one Main SRO number. Several per approval —
    e.g. main 7210.4910 with group numbers 7210.3010, 7210.4990, ..."""
    __tablename__ = "sro_group_numbers"

    group_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    approval_id: Mapped[int] = mapped_column(Integer, ForeignKey('edb_approvals.approval_id', ondelete='CASCADE'),
                         nullable=False, index=True)
    group_sro_no: Mapped[str] = mapped_column(String(50), nullable=False)
    hs_code: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())

    approval = relationship("EdbApproval", back_populates="group_numbers")


class ExBondEntry(Base):
    """One ex-bond lifting against an into-bond GD. The bonded quantity is lifted in parts:
    remaining = into-bond bonded_qty_mt - sum(quantity_mt of its ex-bond entries)."""
    __tablename__ = "ex_bond_entries"

    entry_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    into_bond_gd_id: Mapped[int] = mapped_column(Integer, ForeignKey('goods_declarations.gd_id', ondelete='CASCADE'),
                             nullable=False, index=True)
    shipment_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('shipments.shipment_id', ondelete='SET NULL'),
                         nullable=True)
    # Optional link to a GD record if the ex-bond GD itself was uploaded.
    ex_bond_gd_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('goods_declarations.gd_id', ondelete='SET NULL'),
                           nullable=True)
    gd_number: Mapped[str | None] = mapped_column(String(50))
    ex_bond_date: Mapped[date | None] = mapped_column(Date)
    quantity_mt: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    # Duty is paid when cargo comes OUT of bond, so it is recorded per lifting, not on the
    # into-bond GD. Read off the Ex-Bond GD when it is uploaded, else entered by hand.
    # Feeds "GD previously paid" on the GD Balance report.
    duties_paid_pkr: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    duty_source: Mapped[str | None] = mapped_column(String(20))              # DOCUMENT / MANUAL
    remarks: Mapped[str | None] = mapped_column(Text)
    attachment_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('gd_attachments.attachment_id', ondelete='SET NULL'),
                         nullable=True)
    # Partial GD (business term) / EB Release fields — the Quota Approval this release was
    # validated against, the SRO/quota reference that matched it, and whether the release
    # has completed the Item-Details + approval-validation flow (vs. a legacy manual/simple
    # entry that never goes through that flow).
    approval_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('edb_approvals.approval_id', ondelete='SET NULL'),
                         nullable=True)
    matched_sro_no: Mapped[str | None] = mapped_column(Text)
    is_finalized: Mapped[bool | None] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))

    into_bond_gd = relationship("GoodsDeclaration", back_populates="ex_bond_entries",
                                foreign_keys=[into_bond_gd_id])
    approval = relationship("EdbApproval")
    # Item lines from THIS Partial GD's own Item Details document(s) — never the IB's gd_items.
    items = relationship("ExBondItem", back_populates="entry", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("duty_source IS NULL OR duty_source IN ('DOCUMENT','MANUAL')",
                        name='valid_eb_duty_source'),
    )


class ExBondItem(Base):
    """An item line extracted from a Partial GD's (EB Release's) own Item Details
    document(s) — kept separate from GDItem/gd_items (the IB's own item details) so SRO
    extraction/validation for a release never reads from the IB's item details. A release
    can have several Item Details documents, so several items per entry_id, tagged with
    which attachment produced them."""
    __tablename__ = "ex_bond_items"

    item_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    entry_id: Mapped[int] = mapped_column(Integer, ForeignKey('ex_bond_entries.entry_id', ondelete='CASCADE'),
                   nullable=False, index=True)
    source_attachment_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('gd_attachments.attachment_id', ondelete='SET NULL'),
                         nullable=True)
    item_number: Mapped[int | None] = mapped_column(Integer)
    hs_code: Mapped[str | None] = mapped_column(String(50))
    goods_description: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(15, 3))
    unit: Mapped[str | None] = mapped_column(String(20))
    declared_quantity: Mapped[Decimal | None] = mapped_column(Numeric(15, 3))
    assessed_quantity: Mapped[Decimal | None] = mapped_column(Numeric(15, 3))
    country_of_origin: Mapped[str | None] = mapped_column(String(100))
    sro_no: Mapped[str | None] = mapped_column(Text)
    quota_reference: Mapped[str | None] = mapped_column(Text)
    unit_value_declared: Mapped[Decimal | None] = mapped_column(Numeric(15, 4))
    unit_value_assessed: Mapped[Decimal | None] = mapped_column(Numeric(15, 4))
    total_value_declared_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    total_value_assessed_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    custom_value_declared_pkr: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    custom_value_assessed_pkr: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    m_size: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())

    entry = relationship("ExBondEntry", back_populates="items")


class GdKgtlWeighment(Base):
    """One vehicle trip weighed on KGTL's weighbridge and on our own, recorded against the
    GD once it is closed. The difference (own - KGTL) is derived in kgtl_service, never
    stored, so it can never disagree with the two weights it comes from.

    Weights are in KG — that is what the weighbridge slips print. Reporting converts."""
    __tablename__ = "gd_kgtl_weighments"

    weighment_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    gd_id: Mapped[int] = mapped_column(Integer, ForeignKey('goods_declarations.gd_id', ondelete='CASCADE'),
                   nullable=False, index=True)
    vehicle_number: Mapped[str | None] = mapped_column(String(50))
    weigh_date: Mapped[date | None] = mapped_column(Date)
    own_weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    kgtl_weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    own_coils: Mapped[int | None] = mapped_column(Integer)
    kgtl_coils: Mapped[int | None] = mapped_column(Integer)
    remarks: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id', ondelete='SET NULL'))

    gd = relationship("GoodsDeclaration", back_populates="kgtl_weighments")

    __table_args__ = (
        CheckConstraint(
            "(own_weight_kg IS NULL OR own_weight_kg >= 0) AND "
            "(kgtl_weight_kg IS NULL OR kgtl_weight_kg >= 0)",
            name='valid_kgtl_weights'),
    )


class SroQuotaItem(Base):
    """A single quota line on a GD that draws against an EdbApproval.
    declared vs assessed quantities; consumption = assessed (fallback declared)."""
    __tablename__ = "sro_quota_items"

    item_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    gd_id: Mapped[int] = mapped_column(Integer, ForeignKey('goods_declarations.gd_id', ondelete='CASCADE'),
                   nullable=False, index=True)
    approval_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('edb_approvals.approval_id', ondelete='SET NULL'),
                         nullable=True, index=True)
    hs_code: Mapped[str | None] = mapped_column(String(20))
    part_name: Mapped[str | None] = mapped_column(String(300))
    declared_qty_mt: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    assessed_qty_mt: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())

    gd = relationship("GoodsDeclaration", back_populates="quota_items")
    approval = relationship("EdbApproval", back_populates="quota_items")


class FinancialInstrument(Base):
    """Financial Instrument (FI) from the PSW Portal — the bank's import financial
    instrument. Carries the HS code (matched across documents) and the EXPIRY DATE,
    which is the last date to file the Goods Declaration."""
    __tablename__ = "financial_instruments"

    fi_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    shipment_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('shipments.shipment_id', ondelete='SET NULL'), nullable=True, index=True)
    lc_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('lc_master.lc_id', ondelete='SET NULL'), nullable=True, index=True)

    # Document
    document_filename: Mapped[str | None] = mapped_column(String(255))
    document_path: Mapped[str | None] = mapped_column(Text)
    raw_extracted_data: Mapped[Any | None] = mapped_column(JSONB)
    source: Mapped[str | None] = mapped_column(String(20), default='UPLOADED')

    # Basic information
    fi_number: Mapped[str | None] = mapped_column(String(100), index=True)       # Financial Instrument Unique No.
    fi_status: Mapped[str | None] = mapped_column(String(30))                     # Active / ...
    mode_of_payment: Mapped[str | None] = mapped_column(String(50))               # Letter of Credit
    trader_ntn: Mapped[str | None] = mapped_column(String(20))
    trader_name: Mapped[str | None] = mapped_column(String(300))                  # the importer
    trader_iban: Mapped[str | None] = mapped_column(String(50))
    sight_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))

    # Payment information
    beneficiary_name: Mapped[str | None] = mapped_column(String(300))
    beneficiary_country: Mapped[str | None] = mapped_column(String(100))
    exporter_name: Mapped[str | None] = mapped_column(String(300))
    exporter_country: Mapped[str | None] = mapped_column(String(100))
    delivery_term: Mapped[str | None] = mapped_column(String(50))
    fi_value: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    fi_currency: Mapped[str | None] = mapped_column(String(10))
    exchange_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    lc_contract_no: Mapped[str | None] = mapped_column(String(100), index=True)
    balance: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))

    # Item information
    hs_code: Mapped[str | None] = mapped_column(String(20))
    goods_description: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(15, 3))
    uom: Mapped[str | None] = mapped_column(String(20))
    country_of_origin: Mapped[str | None] = mapped_column(String(100))
    item_invoice_value: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))

    # Financial transaction information
    intended_payment_date: Mapped[date | None] = mapped_column(Date)
    final_date_of_shipment: Mapped[date | None] = mapped_column(Date)
    transport_document_date: Mapped[date | None] = mapped_column(Date)
    expiry_date: Mapped[date | None] = mapped_column(Date, index=True)              # last date to file the GD
    declaration_number: Mapped[str | None] = mapped_column(String(100))           # GD number, once filed

    # Status
    status: Mapped[str | None] = mapped_column(String(20), default='PENDING_REVIEW', index=True)  # PENDING_REVIEW / VERIFIED
    notes: Mapped[str | None] = mapped_column(Text)

    # Audit
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    updated_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))

    # Relationships
    shipment = relationship("Shipment", back_populates="financial_instruments")
    creator = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        CheckConstraint("status IN ('PENDING_REVIEW','VERIFIED')", name='valid_fi_status'),
    )


class InsuranceCertificate(Base):
    """Marine insurance policy / certificate attached to a shipment. Carries the
    BL and LC numbers (cross-checked against the shipment), the certificate/policy
    number and the net premium. AI-extracted from the uploaded PDF."""
    __tablename__ = "insurance_certificates"

    insurance_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    shipment_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('shipments.shipment_id', ondelete='SET NULL'), nullable=True, index=True)
    lc_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('lc_master.lc_id', ondelete='SET NULL'), nullable=True, index=True)

    # Document
    document_filename: Mapped[str | None] = mapped_column(String(255))
    document_path: Mapped[str | None] = mapped_column(Text)
    raw_extracted_data: Mapped[Any | None] = mapped_column(JSONB)
    source: Mapped[str | None] = mapped_column(String(20), default='UPLOADED')

    # Required extracted fields
    bl_number: Mapped[str | None] = mapped_column(String(100), index=True)
    lc_number: Mapped[str | None] = mapped_column(String(100), index=True)
    vessel_name: Mapped[str | None] = mapped_column(String(200))
    certificate_number: Mapped[str | None] = mapped_column(String(100), index=True)   # Insurance Certificate No.
    policy_number: Mapped[str | None] = mapped_column(String(100), index=True)          # Policy No.
    net_premium: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    gross_premium: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))                    # gross premium amount (used for Premium Rate %)

    # Optional extracted fields
    insurance_company: Mapped[str | None] = mapped_column(String(300))
    issue_date: Mapped[date | None] = mapped_column(Date)                                 # Policy issue date
    sum_insured: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))                      # insured amount
    currency: Mapped[str | None] = mapped_column(String(10))
    voyage_route: Mapped[str | None] = mapped_column(String(300))                        # voyage / route (from-to)
    assured_name: Mapped[str | None] = mapped_column(String(300))

    # Status
    status: Mapped[str | None] = mapped_column(String(20), default='PENDING_REVIEW', index=True)  # PENDING_REVIEW / VERIFIED
    notes: Mapped[str | None] = mapped_column(Text)

    # Audit
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    updated_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))

    # Relationships
    shipment = relationship("Shipment", back_populates="insurance_certificates")
    creator = relationship("User", foreign_keys=[created_by])

    __table_args__ = (
        CheckConstraint("status IN ('PENDING_REVIEW','VERIFIED')", name='valid_insurance_status'),
    )


class ShipmentDocument(Base):
    """Record-keeping documents attached to a shipment with NO AI extraction / OCR:
    the DPL document, plus free-form 'other' supporting documents (Delivery Order,
    Undertaking, Custom Letter, Bank Letter, ...). File + name only."""
    __tablename__ = "shipment_documents"

    doc_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    shipment_id: Mapped[int] = mapped_column(Integer, ForeignKey('shipments.shipment_id', ondelete='CASCADE'),
                         nullable=False, index=True)
    doc_kind: Mapped[str] = mapped_column(String(20), nullable=False, default='OTHER')   # DPL / OTHER
    doc_name: Mapped[str | None] = mapped_column(String(200))                                   # display name (e.g. "DPL Document", "Delivery Order")
    document_filename: Mapped[str | None] = mapped_column(String(255))
    document_path: Mapped[str | None] = mapped_column(Text)

    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now())
    uploaded_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    updated_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))

    shipment = relationship("Shipment", back_populates="extra_documents")
    uploader = relationship("User", foreign_keys=[uploaded_by])

    __table_args__ = (
        CheckConstraint("doc_kind IN ('DPL','OTHER')", name='valid_shipment_doc_kind'),
    )


class ActivityLog(Base):
    """User activity / audit trail for shipment documents and important shipment actions
    (who uploaded/replaced/edited a document, who changed status/details, and when)."""
    __tablename__ = "activity_logs"

    log_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    shipment_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('shipments.shipment_id', ondelete='CASCADE'),
                         nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'), nullable=True)
    action: Mapped[str] = mapped_column(String(30), nullable=False)        # UPLOAD / REPLACE / EDIT / STATUS / UPDATE / DELETE
    doc_type: Mapped[str | None] = mapped_column(String(80))                      # e.g. "Bill of Lading", "DPL", "Delivery Order"
    detail: Mapped[str | None] = mapped_column(String(500))                       # freeform, e.g. "status Pending -> Delivered"
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), index=True)

    user = relationship("User", foreign_keys=[user_id])


class SystemAlert(Base):
    """Operational alerts generated by the alert engine (LC expiry, short shipment,
    discrepancy, missing docs, duty surprise, etc.). Distinct from price_alerts (LME)."""
    __tablename__ = "system_alerts"

    alert_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    alert_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # LC_EXPIRING / LAST_SHIP_DATE / SHORT_SHIPMENT / DISCREPANCY /
    # MISSING_DOCUMENT / DUTY_SURPRISE / SHIPMENT_COMPLETE /
    # DEMURRAGE_WARNING / DEMURRAGE_ACCRUING / ARRIVAL_UPCOMING / FI_EXPIRY
    severity: Mapped[str | None] = mapped_column(String(10), default='MEDIUM', index=True)   # HIGH / MEDIUM / LOW
    title: Mapped[str | None] = mapped_column(String(200))
    message: Mapped[str | None] = mapped_column(Text)

    # Linking
    entity_type: Mapped[str | None] = mapped_column(String(20))     # LC / SHIPMENT / GD / BL / FI
    entity_id: Mapped[int | None] = mapped_column(Integer)
    lc_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('lc_master.lc_id', ondelete='CASCADE'), nullable=True, index=True)
    shipment_id: Mapped[int | None] = mapped_column(Integer, ForeignKey('shipments.shipment_id', ondelete='CASCADE'), nullable=True, index=True)

    # Dedup — one active alert per logical event
    dedup_key: Mapped[str | None] = mapped_column(String(120), index=True)

    # Lifecycle
    status: Mapped[str | None] = mapped_column(String(15), default='ACTIVE', index=True)   # ACTIVE / ACKNOWLEDGED / RESOLVED / DISMISSED
    acknowledged_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime)

    # WhatsApp delivery
    whatsapp_sent: Mapped[bool | None] = mapped_column(Boolean, default=False, index=True)
    whatsapp_sent_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=func.now(), index=True)

    __table_args__ = (
        CheckConstraint("severity IN ('HIGH','MEDIUM','LOW')", name='valid_alert_severity'),
        CheckConstraint("status IN ('ACTIVE','ACKNOWLEDGED','RESOLVED','DISMISSED')", name='valid_alert_status'),
        Index('idx_sysalert_active', 'status', 'severity'),
    )


class CurrencyRate(Base):
    __tablename__ = 'currency_rates'

    rate_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rate_date: Mapped[date] = mapped_column(Date, unique=True, nullable=False)
    usd_rate: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    eur_rate: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    aed_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    source: Mapped[str | None] = mapped_column(String(50), default='Manual')
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = ({"schema": SHARED_SCHEMA},)


class ReportTemplate(Base):
    """User-saved report layout — grain, columns, filters and sort for the Report
    Master builder. Ported from rehan/newchanges (backend/models/database_models.py)."""
    __tablename__ = "report_templates"

    template_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('platform.users.user_id', ondelete='CASCADE'), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    grain: Mapped[str] = mapped_column(String(20), nullable=False)             # master | shipment | bl | gd | lc
    columns: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)  # ordered field keys
    filters: Mapped[dict | None] = mapped_column(JSONB, default=dict)          # saved default filters
    sort_field: Mapped[str | None] = mapped_column(String(80))
    sort_dir: Mapped[str | None] = mapped_column(String(4), default='asc')     # asc | desc
    sidebar_order: Mapped[int | None] = mapped_column(Integer, default=0)
    is_builtin: Mapped[bool | None] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    user = relationship("User", foreign_keys=[user_id])
    updated_by: Mapped[int | None] = mapped_column(Integer, ForeignKey('platform.users.user_id'))