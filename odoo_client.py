"""
Odoo XML-RPC client wrapper.

Safety principle used throughout this whole project:
  - Any function that CREATES a record (bill, payment, journal entry) only
    ever calls Odoo's `create` method. It NEVER calls `action_post`,
    `action_confirm`, `mark_as_paid`, or any other method that would move a
    record out of the 'draft' state.
  - This means everything this server creates lands in Odoo as a draft that
    a human still has to open and confirm/post inside Odoo itself.
  - The one exception is `reconcile()`, used only on already-posted lines
    to match existing debits/credits — it moves no money, it only updates
    matching status.
"""

import os
import xmlrpc.client
from typing import Any, Optional


class OdooClient:
    def __init__(
        self,
        url: Optional[str] = None,
        db: Optional[str] = None,
        username: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self.url = (url or os.environ["ODOO_URL"]).rstrip("/")
        self.db = db or os.environ["ODOO_DB"]
        self.username = username or os.environ["ODOO_USERNAME"]
        self.api_key = api_key or os.environ["ODOO_API_KEY"]

        self._common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")
        self._uid = None

    @property
    def uid(self) -> int:
        if self._uid is None:
            self._uid = self._common.authenticate(self.db, self.username, self.api_key, {})
            if not self._uid:
                raise RuntimeError(
                    "Odoo authentication failed. Check ODOO_URL / ODOO_DB / "
                    "ODOO_USERNAME / ODOO_API_KEY."
                )
        return self._uid

    def execute_kw(self, model: str, method: str, args: list, kwargs: Optional[dict] = None) -> Any:
        return self._models.execute_kw(
            self.db, self.uid, self.api_key, model, method, args, kwargs or {}
        )

    def reconcile(self, line_ids: list) -> Any:
        """Reconcile a set of account.move.line ids against each other (full or partial)."""
        return self.execute_kw("account.move.line", "reconcile", [line_ids])

    # ---- generic read helpers ----------------------------------------

    def search_read(self, model: str, domain: list, fields: list, limit: int = 80, order: str = "") -> list:
        kwargs = {"fields": fields, "limit": limit}
        if order:
            kwargs["order"] = order
        return self.execute_kw(model, "search_read", [domain], kwargs)

    def read_group(self, model: str, domain: list, fields: list, groupby: list, limit: int = 200) -> list:
        return self.execute_kw(
            model, "read_group", [domain, fields, groupby], {"lazy": False, "limit": limit}
        )

    def fields_get(self, model: str) -> dict:
        return self.execute_kw(
            model, "fields_get", [], {"attributes": ["string", "type", "relation"]}
        )

    # ---- write helpers (DRAFT ONLY — see module docstring) -----------

    def create(self, model: str, values: dict) -> int:
        """Create a record. Never followed by a call that posts/confirms it."""
        return self.execute_kw(model, "create", [values])
