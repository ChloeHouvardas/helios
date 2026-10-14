"""Emit sample events so `watch` and live refresh can be tested.

Guide § "Developing before other tools are ready": *"A `dev/emit` script fires
`transaction.created` and `obligation.status_changed` events so `watch` and live
refresh can be tested."*

The real Payments and KG population tools emit these events; this script stands
in for them during development. Run it:

    python -m spreadsheets.dev.emit                 # seeds, then emits the set
    python -m spreadsheets.dev.emit --event overdue

Handlers registered with `kg.register_handler(name, fn)` receive every event
whose record matches their subscription query — the same path production uses.
"""

from __future__ import annotations

import argparse
from datetime import timedelta

from ..models import Obligation, Transaction
from ..store import KnowledgeGraph, now
from .seed import seed

# The events the owning tools are contracted to emit (guide § "Contract with the
# Payments and KG tools"): a record created, or a record changing status.
EMITTABLE = ("transaction.created", "obligation.status_changed",
             "obligation.overdue", "obligation.needs_review")


def _split(event: str) -> tuple[str, str]:
    """'transaction.created' -> ('Transaction', 'created')."""
    kind, _, name = event.partition(".")
    return kind.capitalize(), name or "created"


def emit(kg: KnowledgeGraph, event: str = "transaction.created",
         verbose: bool = True) -> dict:
    """Fire one sample event on `kg`. Returns the record it was fired for."""
    record_type, event_name = _split(event)

    if record_type == "Transaction":
        stamp = int(now().timestamp() * 1_000_000)
        tx = Transaction(
            id=f"tx_evt_{stamp}", business_id=kg.business_id,
            source="dev.emit", created_at=now(), amount=12_345_00,
            direction="in", counterparty="Acme Trading Co", channel="wechat",
            occurred_at=now(), channel_ref=f"EVT{stamp}",
            note="INV-2026-0005 settlement arriving")
        kg.add_transaction(tx)          # emits `created`
        emitted = tx
    elif record_type == "Obligation":
        obligations = [o for o in kg.records.get("Obligation", [])
                       if isinstance(o, Obligation)]
        if not obligations:
            raise ValueError("no obligations to emit against; seed first")
        target = obligations[0]
        previous = target.model_dump()
        if event_name == "overdue":
            target.due_at = now() - timedelta(days=1)
            target.status = "open"
            kg.refresh_overdue()        # emits `overdue`
        elif event_name == "needs_review":
            target.needs_review = True
            kg.emit("needs_review", target, previous)
        else:
            # status_changed: move an open obligation to disputed and back out.
            target.status = "disputed" if target.status == "open" else "open"
            kg.emit("status_changed", target, previous)
        emitted = target
    else:
        raise ValueError(f"cannot emit '{event}': unknown record type")

    if verbose:
        delivered = sum(len(s.delivered) for s in kg.subscriptions.values())
        pending = sum(len(s.pending) for s in kg.subscriptions.values())
        print(f"Emitted '{event}' for {record_type} '{emitted.id}'")
        print(f"  subscriptions: {len(kg.subscriptions)}  "
              f"delivered: {delivered}  pending: {pending}")
    return {"event": event, "record_id": emitted.id,
            "record_type": record_type}


def demo(business_id: str = "demo", verbose: bool = True) -> KnowledgeGraph:
    """Seed, subscribe a printing handler, then fire the full event set.

    This is the shortest path to seeing `watch` work end to end.
    """
    kg = seed(business_id, verbose=verbose)

    def printer(msg: dict) -> None:
        rec = msg.get("record", {})
        print(f"  -> handler got '{msg['event']}' for "
              f"{rec.get('id')} ({rec.get('counterparty', '-')})")

    kg.register_handler("dev.printer", printer)
    kg.subscribe({"type": "Transaction"}, ["created"], "dev.printer")
    kg.subscribe({"type": "Obligation"},
                 ["status_changed", "overdue", "needs_review"], "dev.printer")

    if verbose:
        print("\nFiring the sample event set:")
    for event in EMITTABLE:
        emit(kg, event, verbose=verbose)
    return kg


def main() -> None:
    parser = argparse.ArgumentParser(description="Emit sample KG events for watch")
    parser.add_argument("--business", default="demo", help="business_id to seed")
    parser.add_argument("--event", default=None,
                        help=f"emit a single event; one of {EMITTABLE}")
    args = parser.parse_args()

    if args.event:
        kg = seed(args.business, verbose=False)
        emit(kg, args.event)
    else:
        demo(args.business)


if __name__ == "__main__":
    main()
