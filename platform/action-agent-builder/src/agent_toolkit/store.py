"""In-memory fixture store backing the Build 1 mock tools.

Loads customers and invoices from JSON fixture files. Read-only lookups are
used by lookup_invoice and by the audit gate — the gate never trusts the
agent's view of the data, it re-reads the store. Executed send_email calls
mutate the store (outbox plus last-contacted date); those mutations are
recorded in the action log, so a run's state can be rebuilt by replaying
its log (see apply_log).

Fixtures may use absolute dates ("2026-10-11") or dates relative to the day
the store is loaded ("last_contacted_days_ago": 3), which keeps demo and
test scenarios stable over time.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Mapping

DATA_PACKAGE = "agent_toolkit.data"
_RELATIVE_SUFFIX = "_days_ago"


def _load_fixture(name: str) -> dict[str, Any]:
    data_dir = resources.files(DATA_PACKAGE)
    return json.loads(data_dir.joinpath(name).read_text(encoding="utf-8"))


class Store:
    def __init__(
        self,
        customers: Mapping[str, Mapping[str, Any]] | None = None,
        invoices: Mapping[str, Mapping[str, Any]] | None = None,
        today: date | None = None,
    ):
        self._today = today or date.today()
        self._customers: dict[str, dict[str, Any]] = {
            str(customer_id): self._resolve_relative_dates(dict(record))
            for customer_id, record in (customers or {}).items()
        }
        self._invoices: dict[str, dict[str, Any]] = {
            str(invoice_id): self._resolve_relative_dates(dict(record))
            for invoice_id, record in (invoices or {}).items()
        }
        self._sent_emails: list[dict[str, Any]] = []

    @classmethod
    def from_fixtures(cls, today: date | None = None) -> "Store":
        return cls(
            customers=_load_fixture("customers.json"),
            invoices=_load_fixture("invoices.json"),
            today=today,
        )

    @classmethod
    def from_files(
        cls,
        customers_path: Path,
        invoices_path: Path,
        today: date | None = None,
    ) -> "Store":
        with customers_path.open(encoding="utf-8") as handle:
            customers = json.load(handle)
        with invoices_path.open(encoding="utf-8") as handle:
            invoices = json.load(handle)
        return cls(customers=customers, invoices=invoices, today=today)

    def _resolve_relative_dates(self, record: dict[str, Any]) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for key, value in record.items():
            if key.endswith(_RELATIVE_SUFFIX):
                base_field = key[: -len(_RELATIVE_SUFFIX)]
                resolved[base_field] = (self._today - timedelta(days=int(value))).isoformat()
            else:
                resolved[key] = value
        return resolved

    @property
    def today(self) -> date:
        return self._today

    def get_invoice(self, invoice_id: str) -> dict[str, Any] | None:
        invoice = self._invoices.get(invoice_id)
        return dict(invoice) if invoice is not None else None

    def get_customer(self, customer_id: str) -> dict[str, Any] | None:
        customer = self._customers.get(customer_id)
        return dict(customer) if customer is not None else None

    def days_since(self, iso_date: str | None) -> int | None:
        if not iso_date:
            return None
        return (self._today - date.fromisoformat(str(iso_date))).days

    def record_contact(self, customer_id: str, when: date) -> None:
        customer = self._customers.get(customer_id)
        if customer is not None:
            customer["last_contacted"] = when.isoformat()

    def record_sent_email(
        self,
        *,
        customer_id: str,
        invoice_id: str,
        subject: str,
        body: str,
        extension_offer_days: int | None = None,
    ) -> dict[str, Any]:
        customer = self._customers.get(customer_id) or {}
        entry = {
            "message_id": f"msg-{uuid.uuid4().hex[:12]}",
            "customer_id": customer_id,
            "to": customer.get("email"),
            "invoice_id": invoice_id,
            "subject": subject,
            "body": body,
            "extension_offer_days": extension_offer_days,
            "sent_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        self._sent_emails.append(entry)
        self.record_contact(customer_id, self._today)
        return dict(entry)

    @property
    def sent_emails(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self._sent_emails]

    def apply_log_entry(self, entry: Mapping[str, Any]) -> None:
        """Rebuild store state from one action-log entry (see apply_log)."""
        if (
            entry.get("kind") != "action_result"
            or not entry.get("executed")
            or entry.get("tool") != "send_email"
        ):
            return
        result = entry.get("result")
        if isinstance(result, Mapping):
            message_id = result.get("message_id")
            if message_id and any(
                existing.get("message_id") == message_id for existing in self._sent_emails
            ):
                return  # already applied: resume must be safe in the same process too
            self._sent_emails.append(dict(result))
        arguments = entry.get("arguments") or {}
        customer_id = arguments.get("customer_id") or (
            result.get("customer_id") if isinstance(result, Mapping) else None
        )
        if not isinstance(customer_id, str):
            return
        ts = entry.get("ts")
        when = date.fromisoformat(str(ts)[:10]) if ts else self._today
        self.record_contact(customer_id, when)

    def apply_log(self, entries: Iterable[Mapping[str, Any]]) -> None:
        for entry in entries:
            self.apply_log_entry(entry)
