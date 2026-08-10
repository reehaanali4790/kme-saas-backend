"""Aggregates every module's API router into a single api_router, so main.py wires up
one include_router() call instead of importing and registering ~30 routers individually.

Add new module routers here, not in main.py.
"""
from fastapi import APIRouter

from modules.auth.router import router as auth_router
from modules.alerts.router import router as alert_router
from modules.documents.router import router as upload_router
from modules.lc_creation.lc_table_router import router as lc_table_router
from modules.currency_rates.router import router as currency_router
from modules.documents.pdf_upload_router import router as pdf_router
from modules.currency_rates.lme_calculation_router import router as calculation_router
from modules.currency_rates.lme_rates_router import router as lme_rates_router
from modules.currency_rates.lme_web_sync_router import router as lme_web_sync_router
from modules.admin.router import router as admin_router
from modules.shipments.bl_router import router as bl_router
from modules.shipments.router import router as shipment_router
from modules.documents.invoice_router import router as invoice_router
from modules.documents.packing_router import router as packing_router
from modules.weboc.gd_router import router as gd_router
from modules.weboc.router import (
    gd_view_router, item_details_router, into_bond_gd_router, ex_bond_gd_router,
    weboc_router,
)
from modules.weboc.partial_gd_router import partial_gd_router
from modules.documents.fi_router import router as fi_router
from modules.documents.insurance_router import router as insurance_router
from modules.shipments.shipment_doc_router import router as shipment_doc_router
from modules.reports.router import router as report_router
from modules.shipments.demurrage_router import router as demurrage_router
from modules.shipments.container_detention_router import router as container_detention_router
from modules.weboc.sro_router import router as sro_router
from modules.contracts.router import router as contract_router
from modules.lc_creation.router import router as lc_create_router
from modules.reports.lookup_router import router as lookup_router
from modules.alerts.engine_router import router as alert_engine_router
from modules.admin.assistant_endpoints import router as assistant_router
from modules.reports.dashboard_router import router as dashboard_router
from modules.bank_limits.router import router as bank_limit_router
from modules.shipments.vessel_tracker_router import router as vessel_tracker_router
from modules.alerts.expiries_router import router as expiries_router
from modules.finance.roi_router import router as roi_router
from modules.reports.report_master_router import router as report_master_router, router_underscore as report_master_underscore_router
from modules.branding.router import router as branding_router
from modules.billing.router import router as billing_router
from modules.platform.router import router as platform_router

api_router = APIRouter()

for _module_router in (
    auth_router, billing_router, platform_router, alert_router, upload_router, lc_table_router, currency_router,
    pdf_router, calculation_router, lme_rates_router, lme_web_sync_router, admin_router, bl_router,
    shipment_router, invoice_router, packing_router, gd_router, gd_view_router,
    item_details_router, into_bond_gd_router, ex_bond_gd_router, weboc_router,
    partial_gd_router,
    fi_router, insurance_router, shipment_doc_router, report_router,
    demurrage_router, container_detention_router, sro_router, contract_router,
    lc_create_router, lookup_router,
    alert_engine_router, assistant_router, dashboard_router, bank_limit_router,
    vessel_tracker_router, expiries_router, roi_router, report_master_router, report_master_underscore_router,
    branding_router, billing_router, platform_router,
):
    api_router.include_router(_module_router)
