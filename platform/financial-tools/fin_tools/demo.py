"""End-to-end demo: simulate -> verify intent -> shadow preview -> pay -> email -> graph.

Run:  python -m fin_tools.demo   (from platform/financial-tools/)
"""
from __future__ import annotations

from pathlib import Path

from . import build_world
from .intent_verification import VerificationError
from .knowledge_graph import GraphWriteError
from .simulation import PayerProfile, SimPolicy, sandbox_run, what_if
from .spreadsheet_tools import load_csv

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def main() -> None:
    bundle = build_world()
    invoices = load_csv(FIXTURES / "invoices.csv")
    print(f"loaded {len(invoices)} invoices from fixtures/invoices.csv")

    # 1) simulate outcomes ---------------------------------------------------
    profiles = {
        "acme": PayerProfile(customer="acme", mean_delay_days=2, delay_sd_days=1),
        "borealis": PayerProfile(customer="borealis", mean_delay_days=8,
                                 delay_sd_days=4, p_partial=0.3),
        "cinder": PayerProfile(customer="cinder", mean_delay_days=12,
                               delay_sd_days=6, p_dispute=0.2,
                               reminder_responsiveness=0.8),
        "delta-mills": PayerProfile(customer="delta-mills", mean_delay_days=4,
                                    delay_sd_days=2),
    }
    compare = what_if(invoices, profiles,
                      baseline=SimPolicy(reminder_advance_days=0),
                      variant=SimPolicy(reminder_advance_days=3),
                      n_runs=200, seed=7)
    b, v, d = compare["baseline"], compare["variant"], compare["delta"]
    print(f"forecast 30d: baseline {b['expected_within_horizon_cents']/100:,.2f} "
          f"vs remind-3d-early {v['expected_within_horizon_cents']/100:,.2f} "
          f"(delta {d['expected_within_horizon_cents']/100:+,.2f})")

    # 2) shadow preview of a reminder email ----------------------------------
    preview = sandbox_run(bundle.world, bundle.intents, "email.send_template",
                          to="bills@cinder.example", template="payment_reminder",
                          invoice_id="INV-1006", due_date="2026-09-30",
                          contact="Cinder AP", amount="750.00")
    print(f"shadow preview: would send {preview.effects['new_emails']} email(s); "
          f"real outbox still {len(bundle.world.outbox)}")
    bundle.email.send_template(to="bills@cinder.example",
                               template="payment_reminder",
                               invoice_id="INV-1006", due_date="2026-09-30",
                               contact="Cinder AP", amount="750.00")
    print(f"real send: outbox now {len(bundle.world.outbox)}")

    # 3) intent-verified payment ----------------------------------------------
    intent = bundle.intents.capture_intent(
        principal="chloe", amount_cents=480000, currency="CNY",
        payee="supplier-x", purpose="INV-1003 settlement")
    print("intent captured:", bundle.intents.summarize(intent))
    code = bundle.intents.confirm(intent.intent_id, by="chloe").code

    tampered = bundle.payments.send_payment(
        payer="helios-op", payee="supplier-x", amount_cents=480000 + 1,
        currency="CNY", auth_code=code, memo="inflated by 1 cent")
    print(f"tampered amount -> {tampered['state']}: {tampered['reject_reason']}")

    unauth = bundle.payments.send_payment(
        payer="helios-op", payee="supplier-x", amount_cents=480000, currency="CNY")
    print(f"no auth code    -> {unauth['state']}: {unauth['reject_reason']}")

    ok = bundle.payments.send_payment(
        payer="helios-op", payee="supplier-x", amount_cents=480000,
        currency="CNY", auth_code=code, memo="INV-1003 settlement")
    print(f"verified payment -> {ok['state']}; supplier-x balance "
          f"{bundle.payments.balance('supplier-x')/100:,.2f}")

    reuse = bundle.payments.send_payment(
        payer="helios-op", payee="supplier-x", amount_cents=480000,
        currency="CNY", auth_code=code, memo="replay attempt")
    print(f"replayed code   -> {reuse['state']}: {reuse['reject_reason']}")

    # 4) knowledge graph provenance --------------------------------------------
    inv_node = bundle.graph.upsert_verified(
        "invoice", "INV-1003", {"amount_cents": 480000, "customer": "borealis"},
        source="fixtures/invoices.csv")
    node = bundle.graph.upsert_verified(
        "payment", ok["payment_id"], {"amount_cents": 480000}, source="ledger")
    bundle.graph.link(node["node_id"], "settles", inv_node["node_id"])
    try:
        bundle.graph.add_observation("payment", ok["payment_id"],
                                     {"amount_cents": 1}, observer="collect-agent")
        print("graph: UNEXPECTED overwrite of verified fact")
    except GraphWriteError as exc:
        print(f"graph: observation blocked ({exc})")

    # 5) registry view -----------------------------------------------------------
    side_effecting = [t.name for t in bundle.registry.list(side_effects=True)]
    print(f"registry: {len(bundle.registry.list())} tools; "
          f"side-effecting -> {side_effecting}")
    print("demo OK")


if __name__ == "__main__":
    main()
