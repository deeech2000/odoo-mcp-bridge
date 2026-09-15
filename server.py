"""
Odoo <-> Claude MCP bridge.

Exposes a small set of read tools (vendor list, vendor statement/ledger,
open bills, bank journals) and DRAFT-ONLY write tools (create a vendor bill,
create a vendor payment). Nothing this server does can post, confirm, or
pay anything inside Odoo — every created record is left in Odoo's normal
'draft' state for a human to review and confirm inside Odoo itself. The one
exception is reconcile_move_lines, which only matches already-posted lines
against each other and moves no money.

Auth: every request must include header  X-API-Key: <MCP_SHARED_SECRET>
This is separate from the Odoo API key — it's a secret you invent yourself
to stop random people who find your Render URL from calling your tools.
"""

import os
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from odoo_client import OdooClient

_public_host = os.environ.get("PUBLIC_HOSTNAME", "odoo-mcp-bridge.onrender.com")
_transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[_public_host, "127.0.0.1:*", "localhost:*"],
    allowed_origins=[f"https://{_public_host}"],
)

mcp = FastMCP("odoo-accounting-bridge", transport_security=_transport_security)
odoo = OdooClient()

SHARED_SECRET = os.environ.get("MCP_SHARED_SECRET")


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request):
    return PlainTextResponse("ok")


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if SHARED_SECRET and request.url.path not in ("/healthz",):
            if request.headers.get("x-api-key") != SHARED_SECRET:
                return PlainTextResponse("unauthorized", status_code=401)
        return await call_next(request)


# ------------------------------------------------------------------
# READ TOOLS
# ------------------------------------------------------------------

@mcp.tool()
def list_vendors(search: str = "") -> list:
    """List suppliers/vendors in Odoo. Optionally filter by name (partial match)."""
    domain = [("supplier_rank", ">", 0)]
    if search:
        domain.append(("name", "ilike", search))
    return odoo.search_read(
        "res.partner", domain, ["id", "name", "email", "phone"], limit=100, order="name"
    )


@mcp.tool()
def get_vendor_statement(vendor_id: int, limit: int = 100) -> dict:
    """
    Get a vendor's account ledger (statement of account) from Odoo:
    every journal line posted to their payable account, oldest first,
    with a running balance. Positive balance = we owe the vendor.
    """
    lines = odoo.search_read(
        "account.move.line",
        [
            ("partner_id", "=", vendor_id),
            ("account_id.account_type", "=", "liability_payable"),
            ("parent_state", "=", "posted"),
        ],
        ["date", "move_name", "ref", "debit", "credit", "balance", "reconciled"],
        limit=limit,
        order="date asc, id asc",
    )
    running = 0.0
    for line in lines:
        running += line["credit"] - line["debit"]
        line["running_balance"] = round(running, 2)
    return {"vendor_id": vendor_id, "lines": lines, "ending_balance": round(running, 2)}


@mcp.tool()
def list_open_vendor_bills(vendor_id: int = 0) -> list:
    """List posted, unpaid (or partially paid) vendor bills. Pass vendor_id=0 for all vendors."""
    domain = [
        ("move_type", "=", "in_invoice"),
        ("state", "=", "posted"),
        ("payment_state", "in", ["not_paid", "partial"]),
    ]
    if vendor_id:
        domain.append(("partner_id", "=", vendor_id))
    return odoo.search_read(
        "account.move",
        domain,
        ["id", "name", "partner_id", "invoice_date", "invoice_date_due",
         "amount_total", "amount_residual", "payment_state", "ref"],
        limit=200,
        order="invoice_date_due asc",
    )


@mcp.tool()
def list_journal_entries_created_by_user(
    user_search: str,
    date_from: str = "",
    date_to: str = "",
    limit: int = 300,
) -> list:
    """
    List journal entries (bills, customer invoices, payments, manual entries —
    any account.move record) created by a specific Odoo user, newest first.

    user_search: partial match on the user's name or login, e.g. 'Ali' or
                 'a.algahtani@cafcafe.com'. Case-insensitive partial match.
    date_from / date_to: optional 'YYYY-MM-DD' bounds on the entry's date.
    """
    domain = [
        "|",
        ("create_uid.name", "ilike", user_search),
        ("create_uid.login", "ilike", user_search),
    ]
    if date_from:
        domain.append(("date", ">=", date_from))
    if date_to:
        domain.append(("date", "<=", date_to))
    return odoo.search_read(
        "account.move",
        domain,
        ["id", "name", "move_type", "date", "partner_id", "amount_total",
         "state", "ref", "create_date"],
        limit=limit,
        order="date desc, id desc",
    )


@mcp.tool()
def odoo_fields(model: str) -> dict:
    """
    List field names/types for any Odoo model (e.g. 'pos.config', 'pos.order',
    'res.company', 'account.analytic.account'). Use this to discover the
    right field name before calling odoo_read — e.g. to find how "branch"
    or "location" is represented for a given model.
    """
    return odoo.fields_get(model)


@mcp.tool()
def odoo_read(
    model: str,
    domain: list = None,
    fields: list = None,
    group_by: list = None,
    limit: int = 200,
    order: str = "",
) -> list:
    """
    Generic READ-ONLY query against any Odoo model. Read-only: this can
    never create, write, or delete anything — it only ever calls Odoo's
    search_read (or read_group when group_by is given).

    model: e.g. 'pos.order', 'pos.config', 'account.move.line'
    domain: Odoo domain list, e.g. [["date_order", ">=", "2026-04-01"]]
    fields: list of field names to return
    group_by: if given, aggregates with read_group instead of listing records
              (fields should include the ones you want summed, e.g. ["amount_total"])
    """
    domain = domain or []
    fields = fields or []
    if group_by:
        return odoo.read_group(model, domain, fields, group_by, limit=limit)
    return odoo.search_read(model, domain, fields, limit=limit, order=order)


@mcp.tool()
def reconcile_move_lines(line_ids: list) -> dict:
    """
    Reconcile (match) a set of already-POSTED account.move.line ids against
    each other — e.g. matching a vendor payment line against the specific
    bill lines it settles. This does NOT move any money and does NOT create
    or post anything; the payment/bills must already be posted in Odoo. It
    only updates their "reconciled/matched" bookkeeping status.

    IMPORTANT: this only works cleanly if the given lines' debits and
    credits net to zero (a full reconciliation) or are meant as a partial
    match. Always verify the amounts add up before calling this — get the
    line ids and balances from get_vendor_statement first.

    line_ids: list of account.move.line ids (the "id" field returned by
              get_vendor_statement's "lines") to reconcile together.
    """
    result = odoo.reconcile(line_ids)
    return {"line_ids": line_ids, "result": result, "note": "Reconciliation attempted on posted lines."}


@mcp.tool()
def list_bank_journals() -> list:
    """List bank/cash journals available to pay from (needed for creating a draft payment)."""
    return odoo.search_read(
        "account.journal",
        [("type", "in", ["bank", "cash"])],
        ["id", "name", "type", "currency_id"],
        limit=50,
    )


# ------------------------------------------------------------------
# WRITE TOOLS — DRAFT ONLY. Nothing here posts or confirms anything.
# ------------------------------------------------------------------

@mcp.tool()
def create_draft_vendor_bill(
    vendor_id: int,
    invoice_date: str,
    description: str,
    amount: float,
    ref: str = "",
) -> dict:
    """
    Create a DRAFT vendor bill (e.g. for a monthly rent charge) in Odoo.
    It is created in 'draft' state only — it is NOT posted/validated.
    A human must open it in Odoo and confirm it.

    invoice_date: 'YYYY-MM-DD'
    amount: total amount of the single invoice line (before tax)
    """
    move_id = odoo.create(
        "account.move",
        {
            "move_type": "in_invoice",
            "partner_id": vendor_id,
            "invoice_date": invoice_date,
            "ref": ref,
            "invoice_line_ids": [
                (0, 0, {"name": description, "quantity": 1, "price_unit": amount})
            ],
        },
    )
    return {"created_move_id": move_id, "state": "draft", "note": "Not posted. Review in Odoo."}


@mcp.tool()
def create_draft_vendor_payment(
    vendor_id: int,
    amount: float,
    journal_id: int,
    payment_date: str,
    memo: str = "",
) -> dict:
    """
    Create a DRAFT outbound payment to a vendor in Odoo.
    It is created in 'draft' state only — it is NOT posted/confirmed and
    NO money moves. A human must open it in Odoo and confirm/post it
    (and actually send the bank transfer through the bank) themselves.

    payment_date: 'YYYY-MM-DD'
    journal_id: id of the bank/cash journal to pay from — see list_bank_journals()
    """
    payment_id = odoo.create(
        "account.payment",
        {
            "payment_type": "outbound",
            "partner_type": "supplier",
            "partner_id": vendor_id,
            "amount": amount,
            "journal_id": journal_id,
            "date": payment_date,
            "memo": memo,
        },
    )
    return {"created_payment_id": payment_id, "state": "draft", "note": "Not posted. Review in Odoo."}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    app = mcp.streamable_http_app()
    app.add_middleware(ApiKeyMiddleware)
    uvicorn.run(app, host="0.0.0.0", port=port)
