"""Seed the dev KG with realistic sample records.

Guide § "Developing before other tools are ready": *"A `dev/seed` script writes
20 to 30 sample transactions, obligations and counterparties straight into the
KG, covering paid, partial, overdue and disputed cases."*

Run it:

    python -m spreadsheets.dev.seed                # prints a summary
    python -m spreadsheets.dev.seed --business b1  # into a named tenant

The returned `KnowledgeGraph` is what `render_sheet`, `reconcile` and `watch`
read, so a seeded graph is immediately demo-able end to end.
"""

from __future__ import annotations

import argparse
from datetime import timedelta

from ..models import Counterparty, Obligation, Transaction
from ..store import KnowledgeGraph, now

# Counterparties with the aliases a real wallet statement would show. A WeChat
# nickname rarely equals the registered company name — that gap is exactly what
# reconcile step 3 has to bridge.
COUNTERPARTIES = [
    ("Acme Trading Co", ["Acme", "ACME", "深圳艾克米贸易"]),
    ("Beta Logistics", ["Beta", "贝塔物流"]),
    ("Gamma Manufacturing", ["Gamma", "伽马制造"]),
    ("Delta Retail Group", ["Delta", "德尔塔零售"]),
    ("Epsilon Services", ["Epsilon", "艾普西隆服务"]),
]

# (invoice, counterparty, amount in fen, days until due, direction, status)
# Covers every status the guide names: paid, partial, open, overdue, disputed.
OBLIGATIONS = [
    ("INV-2026-0001", "Acme Trading Co", 128_000_00, -45, "receivable", "paid"),
    ("INV-2026-0002", "Acme Trading Co", 64_500_00, -30, "receivable", "partial"),
    ("INV-2026-0003", "Beta Logistics", 32_000_00, -12, "receivable", "overdue"),
    ("INV-2026-0004", "Beta Logistics", 15_750_00, -3, "receivable", "overdue"),
    ("INV-2026-0005", "Gamma Manufacturing", 210_000_00, 7, "receivable", "open"),
    ("INV-2026-0006", "Gamma Manufacturing", 8_900_00, 14, "receivable", "open"),
    ("INV-2026-0007", "Delta Retail Group", 45_600_00, 21, "receivable", "open"),
    ("INV-2026-0008", "Delta Retail Group", 19_999_00, 30, "receivable", "disputed"),
    ("INV-2026-0009", "Epsilon Services", 7_250_00, 45, "receivable", "open"),
    ("INV-2026-0010", "Epsilon Services", 96_000_00, 60, "receivable", "open"),
    ("BILL-2026-0101", "Acme Trading Co", 22_400_00, -8, "payable", "overdue"),
    ("BILL-2026-0102", "Beta Logistics", 5_600_00, 5, "payable", "open"),
    ("BILL-2026-0103", "Gamma Manufacturing", 88_000_00, 12, "payable", "open"),
    ("BILL-2026-0104", "Delta Retail Group", 3_350_00, 19, "payable", "open"),
    ("BILL-2026-0105", "Epsilon Services", 41_000_00, -2, "payable", "overdue"),
]

# (counterparty as it appears on the statement, amount fen, days ago, direction,
#  channel, note). Notes carry invoice references so reconcile step 1 can find
# them, and nicknames so step 3 has something fuzzy to work with.
TRANSACTIONS = [
    ("深圳艾克米贸易", 128_000_00, -40, "in", "wechat", "Payment for INV-2026-0001 谢谢"),
    ("Acme", 30_000_00, -20, "in", "alipay", "INV-2026-0002 partial payment"),
    ("贝塔物流", 32_000_00, -10, "in", "bank", "Settling INV-2026-0003"),
    ("Gamma Manufacturing", 22_400_00, -6, "out", "bank", "BILL-2026-0101 supplier payment"),
    ("艾普西隆服务", 41_000_00, -1, "out", "alipay", "BILL-2026-0105"),
    ("Delta Retail Group", 9_999_00, 0, "in", "wechat", "deposit on INV-2026-0008 (disputed)"),
    ("某供应商", 5_600_00, 0, "out", "bank", "logistics fee"),
]


def seed(business_id: str = "demo", verbose: bool = True) -> KnowledgeGraph:
    """Populate a fresh KG with the sample dataset and return it."""
    kg = KnowledgeGraph(business_id)

    for i, (name, aliases) in enumerate(COUNTERPARTIES, start=1):
        kg.add_counterparty(
            Counterparty(id=f"cp{i:03d}", business_id=business_id,
                         source="dev.seed", created_at=now(), name=name,
                         aliases=list(aliases),
                         payment_details={}),
            emit=False)

    for i, (invoice, cp, amount, days, direction, status) in enumerate(
            OBLIGATIONS, start=1):
        due = (now() + timedelta(days=days)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        kg.add_obligation(
            Obligation(id=f"ob{i:03d}", business_id=business_id,
                       source="dev.seed", created_at=now(), amount=amount,
                       direction=direction, counterparty=cp, due_at=due,
                       invoice_number=invoice, status=status,
                       confidence=0.95),
            emit=False)

    for i, (cp, amount, days, direction, channel, note) in enumerate(
            TRANSACTIONS, start=1):
        kg.add_transaction(
            Transaction(id=f"tx{i:03d}", business_id=business_id,
                        source="dev.seed", created_at=now(), amount=amount,
                        direction=direction, counterparty=cp, channel=channel,
                        occurred_at=now() + timedelta(days=days),
                        channel_ref=f"CH{2026000000 + i}", note=note),
            emit=False)

    if verbose:
        counts = {k: len(v) for k, v in kg.records.items() if v}
        print(f"Seeded business '{business_id}':")
        for name, n in sorted(counts.items()):
            print(f"  {n:3d}  {name}")
        statuses: dict[str, int] = {}
        for ob in kg.records["Obligation"]:
            statuses[ob.status] = statuses.get(ob.status, 0) + 1
        print("  Obligation statuses:", statuses)
        print(f"  Total records: {len(kg.by_id)}")
    return kg


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the dev KG with sample records")
    parser.add_argument("--business", default="demo", help="business_id to seed")
    args = parser.parse_args()
    seed(args.business)


if __name__ == "__main__":
    main()
