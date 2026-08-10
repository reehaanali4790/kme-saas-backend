"""
AI Assistant — natural-language reporting over the database.
Claude is given the schema + a STRICTLY read-only SQL tool (SELECT only, guarded),
so the user can ask for a report of anything and get a plain-language answer.

Safety: only single SELECT/WITH statements; DML/DDL keywords blocked; forced LIMIT;
statement timeout. Intended for authenticated internal users.
"""

import json
import logging
import re

import anthropic
from sqlalchemy import text

from config.database import engine

logger = logging.getLogger("uvicorn")

MODEL = "claude-sonnet-4-6"

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum|merge|call|do)\b",
    re.IGNORECASE,
)

SCHEMA_DOC = """Database schema (PostgreSQL). All money in the currency noted; weights in MT.

lc_master(lc_id, lc_number, contract_number, supplier_name, importer_name, lc_date,
  expiry_date, last_ship_date, currency, status [OPEN/CLOSED/SHIPPED/EXPIRED/CANCELLED],
  bank_name, vessel_name, bl_number)
lc_products(line_id, lc_id, product_code, product_name, origin, quality, quantity,
  unit, lc_unit_price, current_lme, baseline_lme)
shipments(shipment_id, lc_id, shipment_ref, category, status, vessel_name, voyage_number,
  port_of_loading, port_of_discharge, bl_date, eta, total_coils, total_net_weight_mt,
  total_gross_weight_mt, expected_quantity_mt, delivered_quantity_mt, quantity_variance_mt,
  validation_status [PENDING/ALL_CLEAR/DISCREPANT])
bill_of_ladings(bl_id, lc_id, shipment_id, bl_number, bl_date, vessel_name,
  port_of_loading, port_of_discharge, gross_weight_mt, package_count, status)
commercial_invoices(invoice_id, shipment_id, lc_id, invoice_number, invoice_date,
  documentary_credit_number, seller_name, buyer_name, grade, hs_code, unit_price_usd,
  total_net_weight_mt, total_gross_weight_mt, total_coils, total_amount_usd, currency)
invoice_line_items(line_id, invoice_id, item_number, size_thickness_mm, size_width_mm,
  quantity_mt, net_weight_mt, gross_weight_mt, number_of_coils, unit_price_usd, line_amount_usd)
packing_lists(packing_id, shipment_id, packing_number, packing_date, total_net_weight_mt,
  total_gross_weight_mt, total_coils)
goods_declarations(gd_id, shipment_id, lc_id, gd_number, filing_date, hs_code,
  importer_name, exporter_name, declared_value_pkr, assessed_value_pkr, total_duties_pkr,
  customs_duty_pkr, sales_tax_pkr, income_tax_pkr, status)
system_alerts(alert_id, alert_type, severity [HIGH/MEDIUM/LOW], title, message, lc_id,
  shipment_id, status [ACTIVE/ACKNOWLEDGED/DISMISSED], created_at)
currency_rates(rate_id, rate_date, usd_rate, eur_rate)

Notes:
- LC ordered quantity = SUM(lc_products.quantity) per lc_id.
- LC delivered = SUM(shipments.delivered_quantity_mt) per lc_id.
- 'today' is CURRENT_DATE in SQL.
"""

SYSTEM_PROMPT = f"""You are the reporting assistant for a steel-import LC monitoring system.
Answer the user's question by querying the database with the run_sql tool, then giving a
clear, concise answer in plain language. Use tables or bullet points when listing rows.
Always base numbers on actual query results — never invent data.

{SCHEMA_DOC}

Guidelines:
- Write standard PostgreSQL SELECT queries. You may use JOINs, GROUP BY, aggregates, CTEs.
- Keep results focused; the tool auto-limits to 200 rows.
- For money totals, format with thousands separators in your answer.
- If a query returns nothing, say so plainly.
- You may call run_sql multiple times to build a complete answer."""

TOOLS = [{
    "name": "run_sql",
    "description": "Execute a single read-only PostgreSQL SELECT query and return rows.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "A single SELECT statement"}},
        "required": ["query"],
    },
}]


def _run_readonly_sql(query: str) -> dict:
    q = (query or "").strip().rstrip(";").strip()
    low = q.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return {"error": "Only SELECT/WITH queries are allowed."}
    if ";" in q:
        return {"error": "Multiple statements are not allowed."}
    if FORBIDDEN.search(q):
        return {"error": "Query contains a forbidden (write) keyword."}
    if re.search(r"\blimit\b", low) is None:
        q += " LIMIT 200"
    try:
        with engine.connect() as conn:
            conn.execute(text("SET statement_timeout = 8000"))
            result = conn.execute(text(q)).mappings().all()
        rows = [dict(r) for r in result][:200]
        # JSON-safe coercion
        for r in rows:
            for k, v in r.items():
                if hasattr(v, "isoformat"):
                    r[k] = v.isoformat()
                elif isinstance(v, (bytes, bytearray)):
                    r[k] = str(v)
                else:
                    try:
                        json.dumps(v)
                    except TypeError:
                        r[k] = str(v)
        return {"row_count": len(rows), "rows": rows}
    except Exception as e:
        return {"error": f"Query failed: {str(e)[:300]}"}


def ask(question: str, api_key: str, max_turns: int = 6) -> dict:
    """Run the agent loop: Claude queries the DB via run_sql until it answers."""
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not configured.")

    client = anthropic.Anthropic(api_key=api_key)
    messages = [{"role": "user", "content": question}]
    queries_run = []

    for _ in range(max_turns):
        resp = client.messages.create(
            model=MODEL, max_tokens=2048, system=SYSTEM_PROMPT,
            tools=TOOLS, messages=messages,
        )
        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use" and block.name == "run_sql":
                    sql = block.input.get("query", "")
                    queries_run.append(sql)
                    out = _run_readonly_sql(sql)
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": json.dumps(out)[:8000],
                    })
            messages.append({"role": "user", "content": tool_results})
            continue
        # final answer
        answer = "".join(b.text for b in resp.content if b.type == "text")
        logger.info(f"Assistant answered (ran {len(queries_run)} queries)")
        return {"answer": answer, "queries": queries_run}

    return {"answer": "I couldn't complete the report within the step limit. Please refine the question.",
            "queries": queries_run}
