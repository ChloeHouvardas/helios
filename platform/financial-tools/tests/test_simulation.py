import copy

from fin_tools import build_world
from fin_tools.simulation import (PayerProfile, SimPolicy, sandbox_run,
                                  simulate, what_if)

INVOICES = [
    {"invoice_id": "I1", "customer": "acme", "amount_cents": 100000,
     "due_date": "2026-10-20"},
    {"invoice_id": "I2", "customer": "slow", "amount_cents": 200000,
     "due_date": "2026-10-25"},
]
PROFILES = {
    "acme": PayerProfile(customer="acme", mean_delay_days=1, delay_sd_days=0.5),
    "slow": PayerProfile(customer="slow", mean_delay_days=10, delay_sd_days=2,
                         p_dispute=0.0, p_partial=0.0),
}


def test_deterministic_given_seed():
    a = simulate(INVOICES, PROFILES, n_runs=50, seed=3)
    b = simulate(INVOICES, PROFILES, n_runs=50, seed=3)
    assert a.summary == b.summary
    assert a.curve() == b.curve()


def test_different_seed_changes_draws():
    a = simulate(INVOICES, PROFILES, n_runs=50, seed=3)
    b = simulate(INVOICES, PROFILES, n_runs=50, seed=4)
    assert a.runs != b.runs


def test_total_expected_collection_is_bounded_by_booked():
    res = simulate(INVOICES, PROFILES, n_runs=100, seed=1)
    booked = 300000
    assert 0 < res.summary["expected_collected_cents"] <= booked


def test_what_if_common_random_numbers():
    cmp = what_if(INVOICES, PROFILES,
                  baseline=SimPolicy(reminder_advance_days=0),
                  variant=SimPolicy(reminder_advance_days=5),
                  n_runs=100, seed=11)
    assert cmp["variant"]["expected_within_horizon_cents"] >= \
        cmp["baseline"]["expected_within_horizon_cents"]
    assert "delta" in cmp


def test_sandbox_does_not_mutate_real_world():
    bundle = build_world()
    before = copy.deepcopy(bundle.world)
    report = sandbox_run(bundle.world, bundle.intents, "email.send",
                         to="x@y.example", subject="s", body="b")
    assert report.effects["new_emails"] == 1
    assert bundle.world.outbox == before.outbox  # untouched


def test_sandbox_reports_rejection_without_settling():
    bundle = build_world()
    report = sandbox_run(bundle.world, bundle.intents, "payments.send",
                         payer="ops", payee="s", amount_cents=100,
                         currency="CNY")
    assert report.result["state"] == "rejected"
    assert bundle.world.balances == {}


def test_sandbox_unknown_tool():
    bundle = build_world()
    try:
        sandbox_run(bundle.world, bundle.intents, "nope.tool")
        raise AssertionError("should raise")
    except KeyError:
        pass
