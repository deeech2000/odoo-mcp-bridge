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


@mcp.tool()
def validate_bank_statement_lines(
    journal_id: int,
    date_from: str = "",
    date_to: str = "",
    dry_run: bool = True,
) -> dict:
    """
    Auto-match and reconcile unmatched bank statement lines with their Odoo
    payments, by reading the PBNK/BNK reference embedded in each line's
    Label field. This is the 'validate' step after importing a bank
    statement into Odoo.

    Works on all statement lines in the given journal that are NOT yet
    reconciled (is_reconciled = False) and whose Label contains a payment
    reference (e.g. 'PBNK15/2026/00001' or 'BNK1/2026/00918').

    Two-step safety (same pattern as unreconcile_partials):
      dry_run=True  (default): shows what WOULD be matched — line id, date,
                               amount, label and the payment found — without
                               changing anything in Odoo.
      dry_run=False: actually reconciles each matched pair. Only call this
                     after reviewing the dry-run output and confirming it
                     looks correct.

    journal_id: bank journal id (see list_bank_journals()).
    date_from / date_to: optional 'YYYY-MM-DD' bounds on statement line date.
    """
    import re
    domain = [("journal_id", "=", journal_id), ("is_reconciled", "=", False)]
    if date_from:
        domain.append(("date", ">=", date_from))
    if date_to:
        domain.append(("date", "<=", date_to))
    stmt_lines = odoo.search_read(
        "account.bank.statement.line", domain,
        ["id", "date", "payment_ref", "amount", "move_id", "is_reconciled"],
        limit=500, order="date asc",
    )
    PAY_RE = re.compile(r'((?:PBNK|BNK)\d*\/\d{4}\/\d{5,})', re.IGNORECASE)
    results = {"matched": [], "no_payment_ref": [], "payment_not_found": [],
               "already_reconciled": [], "error": []}
    for sl in stmt_lines:
        label = sl.get("payment_ref") or ""
        m = PAY_RE.search(label)
        if not m:
            results["no_payment_ref"].append(
                {"line_id": sl["id"], "date": sl["date"], "amount": sl["amount"], "label": label})
            continue
        pay_ref = m.group(1).upper()
        pays = odoo.search_read(
            "account.payment", [("name", "=", pay_ref)],
            ["id", "name", "move_id", "state"], limit=1,
        )
        if not pays:
            results["payment_not_found"].append(
                {"line_id": sl["id"], "date": sl["date"], "amount": sl["amount"],
                 "label": label, "ref_searched": pay_ref})
            continue
        pay = pays[0]
        results["matched"].append({
            "line_id": sl["id"], "date": sl["date"], "amount": sl["amount"],
            "label": label, "payment": pay["name"], "payment_id": pay["id"],
        })
        if not dry_run:
            try:
                pay_move_lines = odoo.search_read(
                    "account.move.line",
                    [("move_id", "=", pay["move_id"][0]),
                     ("account_id.account_type", "in",
                      ["asset_current", "liability_current",
                       "asset_receivable", "liability_payable"])],
                    ["id", "reconciled", "amount_residual"], limit=10,
                )
                pay_open = [l["id"] for l in pay_move_lines
                            if not l["reconciled"] and abs(l["amount_residual"]) > 0.0001]
                stmt_move_lines = odoo.search_read(
                    "account.move.line",
                    [("move_id", "=", sl["move_id"][0]),
                     ("account_id.account_type", "in",
                      ["asset_current", "liability_current",
                       "asset_receivable", "liability_payable"])],
                    ["id", "reconciled", "amount_residual"], limit=10,
                )
                stmt_open = [l["id"] for l in stmt_move_lines
                             if not l["reconciled"] and abs(l["amount_residual"]) > 0.0001]
                line_ids = pay_open + stmt_open
                if line_ids:
                    odoo.reconcile(line_ids)
                    results["matched"][-1]["status"] = "reconciled"
                else:
                    results["matched"][-1]["status"] = "no_open_lines"
            except Exception as e:
                results["matched"][-1]["status"] = f"error: {e}"
                results["error"].append({"line_id": sl["id"], "error": str(e)})

    summary = {
        "journal_id": journal_id,
        "dry_run": dry_run,
        "total_unreconciled": len(stmt_lines),
        "matched_count": len(results["matched"]),
        "no_payment_ref_count": len(results["no_payment_ref"]),
        "payment_not_found_count": len(results["payment_not_found"]),
        "error_count": len(results["error"]),
    }
    if dry_run:
        summary["next_step"] = (
            f"Re-run with dry_run=False to reconcile the {len(results['matched'])} matched lines."
        )
    return {**summary, **results}


@mcp.tool()
def create_bank_statement_lines(
    journal_id: int,
    lines: list,
) -> dict:
    """
    Upload bank statement lines (حركات كشف البنك) to Odoo as
    account.bank.statement.line records on the given bank journal. Each
    line lands in the journal's "to reconcile" queue — it is NOT yet linked
    to any payment. Use link_statement_line_to_payment afterwards to match
    each line to its Odoo payment.

    journal_id: the bank/cash journal id (see list_bank_journals()).
    lines: list of dicts, one per bank transaction row, each with:
        - date   (str, 'YYYY-MM-DD' or 'DD/MM/YYYY', required)
        - label  (str, required) — the "Label" / Description column
        - amount (float, required) — positive = credit/deposit,
                                     negative = debit/withdrawal
    Returns the created statement line ids.
    """
    import re
    from datetime import datetime
    created = []
    for line in lines:
        raw_date = str(line["date"]).strip()
        if re.match(r'^\d{2}/\d{2}/\d{4}$', raw_date):
            raw_date = datetime.strptime(raw_date, '%d/%m/%Y').strftime('%Y-%m-%d')
        sl_id = odoo.create(
            "account.bank.statement.line",
            {
                "journal_id": journal_id,
                "date": raw_date,
                "payment_ref": str(line.get("label", "")),
                "amount": float(line["amount"]),
            },
        )
        created.append({"line_id": sl_id, "date": raw_date,
                         "label": line.get("label", ""), "amount": line["amount"]})
    return {"created_count": len(created), "lines": created,
            "note": "Lines created in Odoo bank statement queue. Use link_statement_line_to_payment to match each one."}


@mcp.tool()
def link_statement_line_to_payment(
    statement_line_id: int,
    payment_name: str,
) -> dict:
    """
    Match (reconcile) a bank statement line to an existing Odoo payment —
    e.g. link the bank's debit row to PBNK15/2026/00001. This is the step
    that moves the line from 'Uncleared' to 'Cleared' in Odoo's bank
    reconciliation view: the payment's journal line (Uncleared/suspense
    account) is replaced by the actual bank GL account entry from the
    statement line.

    statement_line_id: the id returned by create_bank_statement_lines.
    payment_name: the Odoo payment reference, e.g. 'PBNK15/2026/00001'.
        The tool looks up the payment, finds its open journal line on the
        suspense/bank account, then reconciles it with the statement line's
        counterpart move line.

    Returns is_reconciled_now=True when the link succeeded.
    """
    pay_moves = odoo.search_read(
        "account.payment",
        [("name", "=", payment_name)],
        ["id", "name", "move_id", "state", "amount", "partner_id", "payment_type"],
        limit=1,
    )
    if not pay_moves:
        return {"error": f"Payment '{payment_name}' not found in Odoo."}
    pay = pay_moves[0]
    move_id = pay["move_id"][0]
    pay_lines = odoo.search_read(
        "account.move.line",
        [("move_id", "=", move_id),
         ("account_id.account_type", "in",
          ["asset_receivable", "liability_payable", "asset_current", "liability_current"])],
        ["id", "account_id", "debit", "credit", "amount_residual", "reconciled"],
        limit=10,
    )
    pay_lines_open = [l for l in pay_lines if not l["reconciled"] and abs(l["amount_residual"]) > 0.0001]

    stmt_lines = odoo.search_read(
        "account.bank.statement.line",
        [("id", "=", statement_line_id)],
        ["id", "move_id", "amount", "payment_ref", "is_reconciled"],
        limit=1,
    )
    if not stmt_lines:
        return {"error": f"Statement line {statement_line_id} not found."}
    stmt = stmt_lines[0]
    if stmt["is_reconciled"]:
        return {"error": f"Statement line {statement_line_id} is already reconciled.", "line": stmt}

    stmt_move_id = stmt["move_id"][0]
    stmt_move_lines = odoo.search_read(
        "account.move.line",
        [("move_id", "=", stmt_move_id),
         ("account_id.account_type", "in",
          ["asset_current", "liability_current", "asset_receivable", "liability_payable"])],
        ["id", "account_id", "debit", "credit", "amount_residual", "reconciled"],
        limit=10,
    )
    stmt_open = [l for l in stmt_move_lines if not l["reconciled"] and abs(l["amount_residual"]) > 0.0001]

    line_ids = [l["id"] for l in pay_lines_open] + [l["id"] for l in stmt_open]
    if not line_ids:
        return {"error": "No open lines found to reconcile.",
                "payment_lines": pay_lines, "stmt_lines": stmt_move_lines}

    result = odoo.reconcile(line_ids)
    verify = odoo.search_read(
        "account.bank.statement.line",
        [("id", "=", statement_line_id)],
        ["id", "is_reconciled", "amount_residual"],
        limit=1,
    )
    return {
        "statement_line_id": statement_line_id,
        "payment": payment_name,
        "reconciled_line_ids": line_ids,
        "result": result,
        "is_reconciled_now": verify[0]["is_reconciled"] if verify else None,
        "note": "Statement line linked to payment. Check Odoo bank reconciliation view.",
    }


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
    journal_id: int = 0,
    currency_id: int = 0,
) -> dict:
    """
    Create a DRAFT customer invoice (e.g. franchise royalty billed to a
    franchisee/customer) in Odoo. It is created in 'draft' state only —
    it is NOT posted/validated. A human must open it in Odoo and confirm
    it.

    invoice_date: 'YYYY-MM-DD'
    amount: total amount of the single invoice line (before tax)
    account_id: optional — the GL revenue account for this line.
    branch_analytic_account_id / department_analytic_account_id: optional
        analytic tags (see list_branches() / list_departments()).
    journal_id: optional — the journal to post to (e.g. "CAF X Cherry"
        journal id). Leave as 0 to use the default Customer Invoices journal.
    currency_id: optional — the invoice currency (e.g. USD=2, OMR=4,
        BHD=17, AED=21). Leave as 0 to use the company's default (KWD).
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
    move_vals = {
        "move_type": "out_invoice",
        "partner_id": customer_id,
        "invoice_date": invoice_date,
        "ref": ref,
        "invoice_line_ids": [(0, 0, line_values)],
    }
    if journal_id:
        move_vals["journal_id"] = journal_id
    if currency_id:
        move_vals["currency_id"] = currency_id
    move_id = odoo.create("account.move", move_vals)
    return {"created_move_id": move_id, "state": "draft", "note": "Not posted. Review in Odoo."}


@mcp.tool()
def create_draft_customer_invoice_multiline(
    customer_id: int,
    invoice_date: str,
    ref: str,
    lines: list,
    journal_id: int = 0,
    currency_id: int = 0,
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
    journal_id: optional — the journal to post to (e.g. "CAF X Cherry" journal
        id). Leave as 0 to use the default Customer Invoices journal.
    currency_id: optional — the invoice currency (e.g. USD=2, OMR=4, BHD=17,
        AED=21). Leave as 0 to use the company's default (KWD).
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
    move_vals = {
        "move_type": "out_invoice",
        "partner_id": customer_id,
        "invoice_date": invoice_date,
        "ref": ref,
        "invoice_line_ids": invoice_line_ids,
    }
    if journal_id:
        move_vals["journal_id"] = journal_id
    if currency_id:
        move_vals["currency_id"] = currency_id
    move_id = odoo.create("account.move", move_vals)
    return {"created_move_id": move_id, "state": "draft", "line_count": len(invoice_line_ids),
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
def add_lines_to_draft_bill(
    move_id: int,
    lines: list,
    clear_existing_lines: bool = False,
) -> dict:
    """
    Add invoice lines to an existing DRAFT vendor bill. Refuses if the bill
    is not in draft state. Useful for distributing a marketing/service cost
    across branches by adding one line per branch with its analytic tag.

    move_id: the account.move id of the draft bill.
    lines: list of dicts, each with:
        - description (str, required)
        - amount (float, required) — price_unit (qty always 1)
        - account_id (int, optional) — GL expense account
        - branch_analytic_account_id (int, optional)
        - department_analytic_account_id (int, optional)
    clear_existing_lines: if True, removes all existing invoice lines first
        before adding the new ones. Default False (appends to existing lines).
        Use True when you want to replace a single summary line with the
        per-branch breakdown.
    """
    bills = odoo.search_read(
        "account.move", [("id", "=", move_id)], ["state", "name", "partner_id"], limit=1
    )
    if not bills:
        return {"error": f"Bill {move_id} not found."}
    if bills[0]["state"] != "draft":
        return {"error": f"Bill {move_id} is in state '{bills[0]['state']}', not draft. Refusing to modify."}

    if clear_existing_lines:
        existing = odoo.search_read(
            "account.move.line", [("move_id", "=", move_id), ("display_type", "=", "product")],
            ["id"], limit=200
        )
        if existing:
            odoo.execute_kw("account.move", "write", [[move_id], {
                "invoice_line_ids": [(2, l["id"]) for l in existing]
            }])

    new_lines = []
    for line in lines:
        lv = {"name": line["description"], "quantity": 1, "price_unit": line["amount"]}
        if line.get("account_id"):
            lv["account_id"] = line["account_id"]
        dist = {}
        if line.get("branch_analytic_account_id"):
            dist[str(line["branch_analytic_account_id"])] = 100.0
        if line.get("department_analytic_account_id"):
            dist[str(line["department_analytic_account_id"])] = 100.0
        if dist:
            lv["analytic_distribution"] = dist
        new_lines.append((0, 0, lv))

    odoo.execute_kw("account.move", "write", [[move_id], {"invoice_line_ids": new_lines}])

    updated = odoo.search_read(
        "account.move", [("id", "=", move_id)], ["name", "amount_total", "state"], limit=1
    )
    return {
        "move_id": move_id,
        "bill_name": bills[0]["name"],
        "lines_added": len(lines),
        "cleared_existing": clear_existing_lines,
        "new_total": updated[0]["amount_total"] if updated else None,
        "state": "draft",
        "note": "Lines added. Bill remains draft — review and post in Odoo.",
    }


@mcp.tool()
def create_draft_vendor_bill_multiline(
    vendor_id: int,
    invoice_date: str,
    ref: str,
    lines: list,
) -> dict:
    """
    Create a DRAFT vendor bill with MULTIPLE lines in one go — e.g. one
    car rent invoice distributed across branches by sales percentage, each
    branch on its own line with its own analytic tag. Draft only — NOT
    posted/validated; a human confirms it in Odoo.

    invoice_date: 'YYYY-MM-DD', applies to the whole invoice.
    ref: reference for the whole invoice (e.g. "CAR Rent Sep 2026").
    lines: list of dicts, one per line, each with:
        - description (str, required)
        - amount (number, required) — price_unit (qty is always 1)
        - account_id (int, optional) — GL expense account
        - branch_analytic_account_id (int, optional)
        - department_analytic_account_id (int, optional)
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
            "move_type": "in_invoice",
            "partner_id": vendor_id,
            "invoice_date": invoice_date,
            "ref": ref,
            "invoice_line_ids": invoice_line_ids,
        },
    )
    return {"created_move_id": move_id, "state": "draft", "line_count": len(invoice_line_ids),
            "note": "Not posted. Review in Odoo."}


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
