"""Simulating outcomes.

Two complementary simulators (see research/README.md §2):

A) **Behavioural cash-flow simulation** — persona-driven Monte-Carlo. Each
   `PayerProfile` is a pocket-scale "interview card" (pay-latency distribution,
   dispute/partial-payment habits, reminder responsiveness) in the spirit of
   Park et al.'s grounded personas (arXiv:2411.10109). Rolling profiles over an
   invoice book yields a cash-in distribution and what-if comparisons
   ("remind 3 days earlier"), which is exactly Helios Cash Planner's question.

B) **Shadow-execution sandbox** — ToolEmu's idea (arXiv:2309.15817) in
   deterministic form: run a proposed side-effecting call against a deep copy of
   the world and report the would-be effects without touching the real one.
   `EmulatedSandbox` (protocol) is the documented seam for a future LM emulator.
"""
from __future__ import annotations

import copy
import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional, Protocol

from .contracts import REGISTRY, World
from .email_sender import EmailSender
from .payments import Payments


# ---------------------------------------------------------------------------
# A) behavioural simulation
# ---------------------------------------------------------------------------

@dataclass
class PayerProfile:
    """Parametric stand-in for an interview-grounded persona."""
    customer: str
    mean_delay_days: float = 5.0      # days after due date they actually pay
    delay_sd_days: float = 3.0
    p_dispute: float = 0.05           # chance they raise a dispute first
    dispute_days: int = 14
    p_partial: float = 0.10           # chance they pay in installments
    partial_fraction: float = 0.5     # first instalment share
    second_instalment_days: int = 21
    reminder_responsiveness: float = 0.6   # 0..1, share of reminder advance recovered


@dataclass
class SimPolicy:
    reminder_advance_days: int = 0    # 0 = no reminders
    horizon_days: int = 60


@dataclass
class SimResult:
    runs: List[Dict[int, int]]                 # run -> {day_offset: cents}
    policy: SimPolicy
    summary: Dict[str, Any] = field(default_factory=dict)

    def curve(self) -> Dict[int, int]:
        """Expected cash-in per day, averaged over runs."""
        if not self.runs:
            return {}
        acc: Dict[int, int] = {}
        for run in self.runs:
            for day, cents in run.items():
                acc[day] = acc.get(day, 0) + cents
        return {d: c // len(self.runs) for d, c in sorted(acc.items())}


def _parse(iso: str) -> date:
    return date.fromisoformat(iso)


def simulate(invoices: List[Dict[str, Any]], profiles: Dict[str, PayerProfile],
             *, policy: Optional[SimPolicy] = None, n_runs: int = 200,
             seed: int = 7, today: Optional[date] = None) -> SimResult:
    """Roll each invoice against its payer profile `n_runs` times."""
    policy = policy or SimPolicy()
    today = today or date(2026, 10, 14)
    rng = random.Random(seed)
    runs: List[Dict[int, int]] = []

    for _ in range(n_runs):
        day_cash: Dict[int, int] = {}
        for inv in invoices:
            profile = profiles.get(inv["customer"])
            if profile is None:
                profile = PayerProfile(customer=inv["customer"])
            due = _parse(inv["due_date"])
            due_offset = (due - today).days
            delay = max(0, int(round(rng.gauss(profile.mean_delay_days,
                                               profile.delay_sd_days))))
            amount = int(inv["amount_cents"])

            pay_day = due_offset + delay
            paid = amount
            if rng.random() < profile.p_dispute:
                pay_day += profile.dispute_days
            if policy.reminder_advance_days > 0:
                reminder_day = due_offset - policy.reminder_advance_days
                if pay_day > reminder_day:
                    pay_day -= int(round(policy.reminder_advance_days
                                         * profile.reminder_responsiveness))
            pay_day = max(pay_day, 0)

            if rng.random() < profile.p_partial:
                first = int(amount * profile.partial_fraction)
                day_cash[pay_day] = day_cash.get(pay_day, 0) + first
                day_cash[pay_day + profile.second_instalment_days] = (
                    day_cash.get(pay_day + profile.second_instalment_days, 0)
                    + (amount - first))
            else:
                day_cash[pay_day] = day_cash.get(pay_day, 0) + paid
        runs.append(day_cash)

    totals = sorted(sum(r.get(d, 0) for d in r) for r in runs)
    booked = sum(int(i["amount_cents"]) for i in invoices)

    def pct(q: float) -> int:
        if not totals:
            return 0
        idx = min(len(totals) - 1, max(0, int(round(q * (len(totals) - 1)))))
        return totals[idx]

    within = [sum(c for d, c in r.items() if d <= policy.horizon_days) for r in runs]
    result = SimResult(runs=runs, policy=policy)
    result.summary = {
        "booked_cents": booked,
        "expected_collected_cents": sum(totals) // max(1, len(totals)),
        "p10_cents": pct(0.10), "p50_cents": pct(0.50), "p90_cents": pct(0.90),
        "expected_within_horizon_cents": sum(within) // max(1, len(within)),
        "horizon_days": policy.horizon_days,
        "n_runs": n_runs, "seed": seed,
    }
    return result


def what_if(invoices: List[Dict[str, Any]], profiles: Dict[str, PayerProfile],
            *, baseline: SimPolicy, variant: SimPolicy,
            n_runs: int = 200, seed: int = 7) -> Dict[str, Any]:
    """Compare two policies on identical random draws (common random numbers)."""
    a = simulate(invoices, profiles, policy=baseline, n_runs=n_runs, seed=seed)
    b = simulate(invoices, profiles, policy=variant, n_runs=n_runs, seed=seed)
    diff = {k: b.summary[k] - a.summary[k]
            for k in ("expected_collected_cents", "expected_within_horizon_cents",
                      "p10_cents", "p50_cents") if k in a.summary and k in b.summary}
    return {"baseline": a.summary, "variant": b.summary, "delta": diff}


# ---------------------------------------------------------------------------
# B) shadow-execution sandbox (ToolEmu-style, deterministic)
# ---------------------------------------------------------------------------

class EmulatedSandbox(Protocol):
    """Seam for a future LM-emulated sandbox (ToolEmu). Not implemented here."""
    def emulate(self, tool_name: str, kwargs: Dict[str, Any]) -> Dict[str, Any]: ...


@dataclass
class ShadowReport:
    tool: str
    executed: bool
    result: Any
    effects: Dict[str, Any]


def snapshot_effects(world: World) -> Dict[str, Any]:
    return {
        "payments": len(world.ledger),
        "emails": len(world.outbox),
        "balances": dict(world.balances),
    }


def diff_effects(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    deltas = {k: after["balances"].get(k, 0) - v
              for k, v in before["balances"].items()}
    for k, v in after["balances"].items():
        if k not in before["balances"]:
            deltas[k] = v
    return {
        "new_payments": after["payments"] - before["payments"],
        "new_emails": after["emails"] - before["emails"],
        "balance_deltas_cents": {k: v for k, v in deltas.items() if v},
    }


def sandbox_run(world: World, intents, tool: str, **kwargs: Any) -> ShadowReport:
    """Preview a side-effecting call on a deep copy of the world.

    The real `world` is never mutated. Returns what *would* happen, including
    rejections (e.g. missing intent auth code), so an agent can show the human
    the consequence before committing.
    """
    shadow_world = copy.deepcopy(world)
    shadow_intents = copy.deepcopy(intents)
    payments = Payments(world=shadow_world, intents=shadow_intents)
    email = EmailSender(world=shadow_world)
    before = snapshot_effects(shadow_world)

    if tool == "payments.send":
        result = payments.send_payment(**kwargs)
    elif tool == "payments.receive":
        result = payments.receive_payment(**kwargs)
    elif tool == "email.send":
        result = email.send(**kwargs)
    elif tool == "email.send_template":
        result = email.send_template(**kwargs)
    else:
        raise KeyError(f"sandbox does not emulate tool: {tool}")

    after = snapshot_effects(shadow_world)
    return ShadowReport(tool=tool, executed=True, result=result,
                        effects=diff_effects(before, after))


#: Anchor date for the compatibility forecast. The old persona `simulate()`
#: defaulted `today` to this; keeping it means the delegation reproduces the same
#: horizon window the tool always used.
_COMPAT_TODAY = date(2026, 10, 14)


def _profile_offset_days(profile: "PayerProfile", reminder_advance_days: int) -> List[List[float]]:
    """Discretise a persona's pay-delay into the engine's `offset_days` weights.

    The persona model is a Gaussian delay (plus a dispute shift and an earlier
    payment when a reminder is sent); the engine wants a discrete
    [[days, weight], ...] distribution. Sampling a +/-3sd grid and weighting by
    the Gaussian pdf preserves the mean delay, and folding the dispute branch in
    as extra weighted buckets preserves its expected effect. A reminder pulls
    every bucket earlier by `advance * reminder_responsiveness`, matching the old
    model. This is an approximation of the distribution, but it is exact in
    expected value, which is what a cash-in curve reports.
    """
    mean = float(profile.mean_delay_days)
    sd = float(profile.delay_sd_days)
    advance = int(round(reminder_advance_days * profile.reminder_responsiveness)) \
        if reminder_advance_days else 0
    buckets: Dict[int, float] = {}

    def add(day: int, weight: float) -> None:
        day = max(0, day - advance)          # a reminder pulls payment earlier
        buckets[day] = buckets.get(day, 0.0) + weight

    if sd <= 0:
        base = int(round(mean))
        add(base, 1.0 - profile.p_dispute)
        add(base + profile.dispute_days, profile.p_dispute)
    else:
        lo, hi = int(math.floor(mean - 3 * sd)), int(math.ceil(mean + 3 * sd))
        for d in range(lo, hi + 1):
            w = math.exp(-0.5 * ((d - mean) / sd) ** 2)
            base = max(0, d)
            add(base, w * (1.0 - profile.p_dispute))
            add(base + profile.dispute_days, w * profile.p_dispute)

    total = sum(buckets.values()) or 1.0
    # Drop buckets whose normalised weight rounds to zero: the Gaussian tail
    # produces arbitrarily small weights and the engine rejects a non-positive
    # one. The dropped mass is negligible, so the expected delay is unchanged.
    return [[d, w / total] for d, w in sorted(buckets.items())
            if w / total > 0.0]


def _invoice_flows(invoices: List[Dict[str, Any]], profiles: Dict[str, "PayerProfile"],
                   reminder_advance_days: int) -> List[Dict[str, Any]]:
    """Translate a persona invoice book into cents-shaped engine inflow flows.

    Expected value is preserved exactly. A partial-payer becomes two flows: a
    near flow whose amount distribution is full-vs-first-instalment, and a
    deferred remainder flow gated by `p_partial`. The engine draws flows
    independently, so the two-instalment case is an approximation of the
    correlated original in the tails while matching it in expectation — noted
    because `simulation.forecast` is a compatibility shim, and the authoritative
    interfaces are now `forecast.simulate` / `forecast.project`.
    """
    flows: List[Dict[str, Any]] = []
    for i, inv in enumerate(invoices):
        profile = profiles.get(inv["customer"]) or PayerProfile(customer=inv["customer"])
        amount = int(inv["amount_cents"])
        due = inv["due_date"]
        fid = str(inv.get("invoice_id", f"inv-{i}"))
        offset = _profile_offset_days(profile, reminder_advance_days)
        near: Dict[str, Any] = {
            "id": fid, "account": "main", "direction": "inflow",
            "date": {"due": due, "offset_days": offset},
            "customer_id": inv["customer"],
        }
        if profile.p_partial > 0:
            first = int(amount * profile.partial_fraction)
            near["amount_distribution_cents"] = [
                [amount, 1.0 - profile.p_partial], [first, profile.p_partial]]
            flows.append(near)
            deferred_offset = [[d + profile.second_instalment_days, w]
                               for d, w in offset]
            flows.append({
                "id": f"{fid}-rest", "account": "main", "direction": "inflow",
                "amount_cents": amount - first, "probability": profile.p_partial,
                "date": {"due": due, "offset_days": deferred_offset},
                "customer_id": inv["customer"],
            })
        else:
            near["amount_cents"] = amount
            flows.append(near)
    return flows


def register(registry=REGISTRY,
             profiles: Optional[Dict[str, PayerProfile]] = None) -> None:
    """Read-only simulation tools exposed to agents.

    `profiles` is the dict this bundle's payer profiles live in. Closing over it
    (rather than reading the module-level `_PROFILES`) is what keeps two worlds
    in one process from sharing a payer book. Omit it and the deprecated global
    below is used, which is process-wide.
    """
    holder: Dict[str, PayerProfile] = (
        profiles if profiles is not None else _PROFILES)

    @registry.tool(
        "simulation.forecast",
        "Monte-Carlo cash-in forecast over the invoice book using payer profiles. "
        "Read-only. Compatibility shim: the persona book is translated into the "
        "shared forecasting engine's flows and delegated to forecast.project / "
        "forecast.simulate, so there is one Monte-Carlo engine, not two. Money is "
        "integer cents.",
        {"invoices": "list[dict]", "policy": "dict|None", "n_runs": "int|None",
         "seed": "int|None"},
        {"summary": "dict", "curve": "dict[int,int]"},
        side_effects=False, tags=("simulation",),
    )
    def _forecast(invoices: List[Dict[str, Any]],
                  policy: Optional[Dict[str, Any]] = None,
                  n_runs: Optional[int] = 200,
                  seed: Optional[int] = 7) -> Dict[str, Any]:
        from .forecast import forecast_project, forecast_simulate

        pol = SimPolicy(**policy) if policy else SimPolicy()
        runs = n_runs or 200
        rng_seed = 7 if seed is None else seed
        flows = _invoice_flows(invoices, holder, pol.reminder_advance_days)
        start = _COMPAT_TODAY.isoformat()
        booked = sum(int(inv["amount_cents"]) for inv in invoices)

        # Expected cash-in curve from the deterministic projection; the
        # distribution summary from the seeded Monte Carlo. Both run the shared
        # engine over the same translated flows.
        proj = forecast_project(opening=[], flows=flows, start=start,
                                days=pol.horizon_days)
        sim = forecast_simulate(opening=[], flows=flows, start=start,
                                days=pol.horizon_days, n_paths=runs, seed=rng_seed,
                                percentile_levels=[10, 50, 90])
        final = {band["level"]: band["values"][-1] for band in sim["percentiles"]}
        curve = {day: cents for day, cents in enumerate(proj["inflows_cents"])}
        summary = {
            "booked_cents": booked,
            "expected_collected_cents": proj["expected_inflows_cents"],
            "p10_cents": final.get(10, 0),
            "p50_cents": final.get(50, 0),
            "p90_cents": final.get(90, 0),
            "expected_within_horizon_cents": proj["expected_inflows_cents"],
            "horizon_days": pol.horizon_days,
            "n_runs": runs, "seed": rng_seed,
        }
        return {"summary": summary, "curve": curve}


#: Deprecated process-global payer profiles. `build_world()` no longer uses
#: this; each bundle carries its own dict. Kept so `set_profiles()` callers
#: from before the per-bundle change still behave as they did.
_PROFILES: Dict[str, PayerProfile] = {}


def set_profiles(profiles: Dict[str, PayerProfile]) -> None:
    """Deprecated: set the process-global payer profiles.

    Mutating global state means the last caller wins for every bundle that was
    built without its own profiles dict. Set `bundle.profiles` instead.
    """
    _PROFILES.clear()
    _PROFILES.update(profiles)


def _profiles() -> Dict[str, PayerProfile]:
    return _PROFILES
