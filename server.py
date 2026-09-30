"""
Odoo <-> Claude MCP bridge.

Exposes a small set of read tools (vendor list, vendor statement/ledger,
open bills, bank journals) and DRAFT-ONLY write tools (create a vendor bill,
create a vendor payment). Nothing this server does can post, confirm, or
pay anything inside Odoo — every created record is left in Odoo's normal
'draft' state for a human to review and confirm inside Odoo itself.

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
def list_partial_reconciles(
    created_from: str = "",
    created_to: str = "",
    created_by_user_id: int = 0,
    id_from: int = 0,
    id_to: int = 0,
    limit: int = 500,
) -> list:
    """
    READ-ONLY. List reconciliation matches (account.partial.reconcile) so a wrong
    batch of matches can be found before undoing it. Each row shows when/who
    created it, the amount, the payment/debit line and the bill/credit line it
    links, the PARTNER on each side, and `cross_partner` = true when the two sides
    belong to different partners (a strong sign of a wrong match).

    created_from / created_to: 'YYYY-MM-DD HH:MM:SS' as stored by Odoo (UTC).
    created_by_user_id: res.users id (0 = anyone).
    id_from / id_to: optional bounds on the match id (0 = no bound).
    """
    domain = []
    if created_from:
        domain.append(("create_date", ">=", created_from))
    if created_to:
        domain.append(("create_date", "<=", created_to))
    if created_by_user_id:
        domain.append(("create_uid", "=", created_by_user_id))
    if id_from:
        domain.append(("id", ">=", id_from))
    if id_to:
        domain.append(("id", "<=", id_to))
    partials = odoo.search_read(
        "account.partial.reconcile", domain,
        ["id", "create_date", "create_uid", "amount", "debit_move_id", "credit_move_id", "full_reconcile_id"],
        limit=limit, order="id asc",
    )
    line_ids = set()
    for p in partials:
        for k in ("debit_move_id", "credit_move_id"):
            if p.get(k):
                line_ids.add(p[k][0])
    partner_of = {}
    if line_ids:
        lines = odoo.search_read(
            "account.move.line", [("id", "in", sorted(line_ids))], ["partner_id"], limit=len(line_ids)
        )
        partner_of = {l["id"]: (l["partner_id"][1] if l.get("partner_id") else None) for l in lines}
    for p in partials:
        dp = partner_of.get(p["debit_move_id"][0]) if p.get("debit_move_id") else None
        cp = partner_of.get(p["credit_move_id"][0]) if p.get("credit_move_id") else None
        p["debit_partner"], p["credit_partner"] = dp, cp
        p["cross_partner"] = bool(dp and cp and dp != cp)
    return partials


@mcp.tool()
def unreconcile_partials(partial_ids: list, dry_run: bool = True, confirm_count: int = 0) -> dict:
    """
    Undo reconciliation matches by id (account.partial.reconcile) using Odoo's own
    unlink, the same as the "Unreconcile" button. This moves NO money and deletes
    NO payment, bill or journal entry — it only removes the match between them, and
    Odoo then recomputes the paid/not-paid state of the affected bills and payments.
    A removed match can be recreated later with reconcile_move_lines.

    Two-step safety:
      1) Call with dry_run=True (default) to see exactly what would be removed
         (count, total amount, how many belong to a full reconciliation, ids not found).
      2) Call again with dry_run=False AND confirm_count = the count from step 1.
    Max 500 ids per call. Only pass the exact ids you verified with
    list_partial_reconciles — never a guessed id range, because ids can have gaps and
    neighbouring ids may belong to other users (e.g. POS sessions).
    """
    if not partial_ids:
        return {"error": "partial_ids is empty."}
    if len(partial_ids) > 500:
        return {"error": "Max 500 ids per call. Split into batches."}
    partial_ids = sorted({int(i) for i in partial_ids})
    found = odoo.search_read(
        "account.partial.reconcile", [("id", "in", partial_ids)],
        ["id", "amount", "full_reconcile_id", "create_uid", "create_date"], limit=len(partial_ids),
    )
    found_ids = sorted(p["id"] for p in found)
    missing = [i for i in partial_ids if i not in set(found_ids)]
    creators = sorted({(p["create_uid"][1] if p.get("create_uid") else "?") for p in found})
    dates = sorted({p["create_date"] for p in found})
    summary = {
        "matches_found": len(found_ids),
        "total_amount": round(sum(p["amount"] for p in found), 3),
        "in_full_reconciliation": sum(1 for p in found if p.get("full_reconcile_id")),
        "created_by": creators,
        "create_dates": dates[:3] + (["..."] if len(dates) > 3 else []),
        "ids_not_found": missing,
    }
    if dry_run:
        return {"dry_run": True, **summary,
                "next_step": f"Re-run with dry_run=False and confirm_count={len(found_ids)} to execute."}
    if confirm_count != len(found_ids):
        return {"error": f"confirm_count ({confirm_count}) must equal matches_found ({len(found_ids)}). Nothing was removed.",
                **summary}
    odoo.unlink_partial_reconciles(found_ids)
    still = odoo.search_read(
        "account.partial.reconcile", [("id", "in", found_ids)], ["id"], limit=len(found_ids)
    )
    return {"dry_run": False, "removed": len(found_ids) - len(still),
            "still_present": [p["id"] for p in still], **summary}


@mcp.tool()
def list_draft_payments(journal_id: int = 0, date_from: str = "", date_to: str = "", limit: int = 100) -> list:
    """
    List DRAFT (not yet posted) vendor/customer payments. Useful for finding
    payments you've reset to draft that still need their journal (bank
    account), date, or memo corrected before re-posting.

    journal_id: pass a journal id (see list_bank_journals()) to filter to
                payments currently sitting in that bank/cash account. 0 = all.
    date_from / date_to: optional 'YYYY-MM-DD' bounds.
    """
    domain = [("state", "=", "draft")]
    if journal_id:
        domain.append(("journal_id", "=", journal_id))
    if date_from:
        domain.append(("date", ">=", date_from))
    if date_to:
        domain.append(("date", "<=", date_to))
    return odoo.search_read(
        "account.payment",
        domain,
        ["id", "name", "partner_id", "amount", "journal_id", "date", "memo", "payment_type"],
        limit=limit,
        order="date asc",
    )


@mcp.tool()
def update_draft_payment(
    payment_id: int,
    journal_id: int = 0,
    date: str = "",
    memo: str = "",
) -> dict:
    """
    Edit a payment's journal (bank account), date, and/or memo — but ONLY
    while it is still in 'draft' state. Refuses with an error if the
    payment is posted/confirmed, so this can never silently alter a
    payment that has already gone through Odoo's approval flow. Does NOT
    post/confirm the payment — it stays draft for a human to review and
    post themselves in Odoo.

    Only pass the fields you actually want to change; leave others at
    their default (0 / "") to leave them untouched.
    """
    values = {}
    if journal_id:
        values["journal_id"] = journal_id
    if date:
        values["date"] = date
    if memo:
        values["memo"] = memo
    if not values:
        return {"payment_id": payment_id, "note": "Nothing to update — no fields provided."}
    odoo.write_draft_only("account.payment", payment_id, values)
    return {"payment_id": payment_id, "updated_fields": values, "state": "draft",
            "note": "Updated while draft. Not posted — review and post in Odoo yourself."}


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
def list_branches(search: str = "") -> list:
    """
    List the company's branches/locations (Odoo analytic accounts used as
    "Branch" on pos.config). Returns each branch's analytic account id and
    name — use that id as branch_analytic_account_id in
    create_draft_vendor_bill to tag a bill's expense line to a specific
    branch/location.

    search: optional partial match on the branch name.
    """
    domain = []
    if search:
        domain.append(("name", "ilike", search))
    configs = odoo.search_read(
        "pos.config", domain, ["id", "name", "analytic_account_id"], limit=200, order="name"
    )
    seen = {}
    for c in configs:
        acc = c.get("analytic_account_id")
        if acc:
            seen[acc[0]] = acc[1]
    return [{"analytic_account_id": k, "branch_name": v} for k, v in sorted(seen.items(), key=lambda x: x[1])]


@mcp.tool()
def list_departments(search: str = "") -> list:
    """
    List the company's departments (Odoo analytic accounts on the
    "Department" analytic plan — e.g. "Top management", "Operation",
    "Finance"). Use the returned id as department_analytic_account_id in
    create_draft_vendor_bill for HO/department-level costs (as opposed to
    a specific branch).

    search: optional partial match on the department name.
    """
    domain = [("plan_id.name", "=", "Department")]
    if search:
        domain.append(("name", "ilike", search))
    return odoo.search_read(
        "account.analytic.account", domain, ["id", "name"], limit=100, order="name"
    )


@mcp.tool()
def list_expense_accounts(search: str = "") -> list:
    """
    List expense-type GL accounts (account.account), so you can pick the
    right account_id for create_draft_vendor_bill — e.g. "Outlets Rental"
    (51030101) for branch rent vs "Rentals - head office" (51030103) for
    HO rent. Search by name or code.
    """
    domain = [("account_type", "in", ["expense", "expense_direct_cost"])]
    if search:
        domain.append(("name", "ilike", search))
    return odoo.search_read(
        "account.account", domain, ["id", "code", "name", "account_type"], limit=100, order="code"
    )


@mcp.tool()
def list_customers(search: str = "") -> list:
    """List customers in Odoo (e.g. franchisees billed for royalties). Optionally filter by name."""
    domain = [("customer_rank", ">", 0)]
    if search:
        domain.append(("name", "ilike", search))
    return odoo.search_read(
        "res.partner", domain, ["id", "name", "email", "phone"], limit=100, order="name"
    )


@mcp.tool()
def get_customer_statement(customer_id: int, limit: int = 100) -> dict:
    """
    Get a customer's account ledger (statement of account) from Odoo:
    every journal line posted to their receivable account, oldest first,
    with a running balance. Positive balance = the customer owes us.
    """
    lines = odoo.search_read(
        "account.move.line",
        [
            ("partner_id", "=", customer_id),
            ("account_id.account_type", "=", "asset_receivable"),
            ("parent_state", "=", "posted"),
        ],
        ["date", "move_name", "ref", "debit", "credit", "balance", "reconciled"],
        limit=limit,
        order="date asc, id asc",
    )
    running = 0.0
    for line in lines:
        running += line["debit"] - line["credit"]
        line["running_balance"] = round(running, 2)
    return {"customer_id": customer_id, "lines": lines, "ending_balance": round(running, 2)}


@mcp.tool()
def create_draft_customer_invoice(
    customer_id: int,
    invoice_date: str,
    description: str,
    amount: float,
    ref: str = "",
    account_id: int = 0,
    branch_analytic_account_id: int = 0,
    department_analytic_account_id: int = 0,
) -> dict:
    """
    Create a DRAFT customer invoice (e.g. franchise royalty billed to a
    franchisee/customer) in Odoo. It is created in 'draft' state only —
    it is NOT posted/validated. A human must open it in Odoo and confirm
    it.

    invoice_date: 'YYYY-MM-DD'
    amount: total amount of the single invoice line (before tax)
    account_id: optional — the GL revenue account for this line (e.g. a
        royalty income account). Leave as 0 to let Odoo use the default.
    branch_analytic_account_id / department_analytic_account_id: optional
        analytic tags (see list_branches() / list_departments()) — for
        franchise royalties this is often a department like "Franchise"
        rather than a branch.
    """
    line_values = {"name": description, "quantity": 1, "price_unit": amount}
    if account_id:
        line_values["account_id"] = account_id
    analytic_distribution = {}
    if branch_analytic_account_id:
        analytic_distribution[str(branch_analytic_account_id)] = 100.0
    if department_analytic_account_id:
        analytic_distribution[str(department_analytic_account_id)] = 100.0
    if analytic_distribution:
        line_values["analytic_distribution"] = analytic_distribution
    move_id = odoo.create(
        "account.move",
        {
            "move_type": "out_invoice",
            "partner_id": customer_id,
            "invoice_date": invoice_date,
            "ref": ref,
            "invoice_line_ids": [(0, 0, line_values)],
        },
    )
    return {"created_move_id": move_id, "state": "draft", "note": "Not posted. Review in Odoo."}


@mcp.tool()
def create_draft_customer_invoice_multiline(
    customer_id: int,
    invoice_date: str,
    ref: str,
    lines: list,
) -> dict:
    """
    Create a DRAFT customer invoice with MULTIPLE lines in one go — e.g. one
    franchise royalty invoice with a separate line per branch/outlet, all on
    the same invoice. Draft only — NOT posted/validated; a human confirms it
    in Odoo.

    invoice_date: 'YYYY-MM-DD', applies to the whole invoice.
    ref: reference for the whole invoice (e.g. "Egypt Royalty - Sep 2026").
    lines: list of dicts, one per line, each with:
        - description (str, required)
        - amount (number, required) — this line's price_unit (qty is always 1)
        - account_id (int, optional) — GL revenue account for this line
        - branch_analytic_account_id (int, optional)
        - department_analytic_account_id (int, optional)
      e.g. [{"description": "CAF Cafe SA", "amount": 5960.35,
             "branch_analytic_account_id": 158}, ...]
    """
    invoice_line_ids = []
    for line in lines:
        line_values = {"name": line["description"], "quantity": 1, "price_unit": line["amount"]}
        if line.get("account_id"):
            line_values["account_id"] = line["account_id"]
        analytic_distribution = {}
        if line.get("branch_analytic_account_id"):
            analytic_distribution[str(line["branch_analytic_account_id"])] = 100.0
        if line.get("department_analytic_account_id"):
            analytic_distribution[str(line["department_analytic_account_id"])] = 100.0
        if analytic_distribution:
            line_values["analytic_distribution"] = analytic_distribution
        invoice_line_ids.append((0, 0, line_values))

    move_id = odoo.create(
        "account.move",
        {
            "move_type": "out_invoice",
            "partner_id": customer_id,
            "invoice_date": invoice_date,
            "ref": ref,
            "invoice_line_ids": invoice_line_ids,
        },
    )
    return {"created_move_id": move_id, "state": "draft", "line_count": len(lines),
            "note": "Not posted. Review in Odoo."}


@mcp.tool()
def create_draft_customer_credit_note(
    customer_id: int,
    invoice_date: str,
    description: str,
    amount: float,
    ref: str = "",
    account_id: int = 0,
    branch_analytic_account_id: int = 0,
    department_analytic_account_id: int = 0,
) -> dict:
    """
    Create a DRAFT customer credit note / refund invoice (Odoo move_type
    'out_refund') — e.g. to deduct a local withholding tax from a franchise
    royalty invoice. Draft only — NOT posted/validated; a human confirms it
    in Odoo.

    invoice_date: 'YYYY-MM-DD'
    amount: the credit note's line amount (positive number — Odoo treats an
        out_refund's amount as a reduction on its own).
    account_id / branch_analytic_account_id / department_analytic_account_id:
        same optional tagging as create_draft_customer_invoice.
    """
    line_values = {"name": description, "quantity": 1, "price_unit": amount}
    if account_id:
        line_values["account_id"] = account_id
    analytic_distribution = {}
    if branch_analytic_account_id:
        analytic_distribution[str(branch_analytic_account_id)] = 100.0
    if department_analytic_account_id:
        analytic_distribution[str(department_analytic_account_id)] = 100.0
    if analytic_distribution:
        line_values["analytic_distribution"] = analytic_distribution
    move_id = odoo.create(
        "account.move",
        {
            "move_type": "out_refund",
            "partner_id": customer_id,
            "invoice_date": invoice_date,
            "ref": ref,
            "invoice_line_ids": [(0, 0, line_values)],
        },
    )
    return {"created_move_id": move_id, "state": "draft", "note": "Not posted. Review in Odoo."}


@mcp.tool()
def create_draft_vendor_bill_v2(
    vendor_id: int,
    invoice_date: str,
    description: str,
    amount: float,
    ref: str = "",
    account_id: int = 0,
    branch_analytic_account_id: int = 0,
    department_analytic_account_id: int = 0,
) -> dict:
    """
    Create a DRAFT vendor bill (e.g. for a monthly rent charge) in Odoo.
    It is created in 'draft' state only — it is NOT posted/validated.
    A human must open it in Odoo and confirm it.

    invoice_date: 'YYYY-MM-DD'
    amount: total amount of the single invoice line (before tax)
    account_id: optional — the GL expense account for this line (see
        list_expense_accounts()). Leave as 0 to let Odoo use the vendor's
        default expense account.
    branch_analytic_account_id: optional — tags the line to a specific
        branch/location (see list_branches()). Leave as 0 for no branch tag
        (e.g. head-office costs).
    department_analytic_account_id: optional — tags the line to a
        department (see list_departments()), e.g. "Top management" for HO
        costs. Can be combined with branch_analytic_account_id on the same
        line (Odoo supports multiple analytic dimensions at once) or used
        alone without a branch.
    """
    line_values = {"name": description, "quantity": 1, "price_unit": amount}
    if account_id:
        line_values["account_id"] = account_id
    analytic_distribution = {}
    if branch_analytic_account_id:
        analytic_distribution[str(branch_analytic_account_id)] = 100.0
    if department_analytic_account_id:
        analytic_distribution[str(department_analytic_account_id)] = 100.0
    if analytic_distribution:
        line_values["analytic_distribution"] = analytic_distribution
    move_id = odoo.create(
        "account.move",
        {
            "move_type": "in_invoice",
            "partner_id": vendor_id,
            "invoice_date": invoice_date,
            "ref": ref,
            "invoice_line_ids": [(0, 0, line_values)],
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
