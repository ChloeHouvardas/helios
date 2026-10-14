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


def register(registry=REGISTRY) -> None:
    """Read-only simulation tools exposed to agents."""

    @registry.tool(
        "simulation.forecast", "Monte-Carlo cash-in forecast over the invoice "
        "book using payer profiles. Read-only.",
        {"invoices": "list[dict]", "policy": "dict|None", "n_runs": "int|None",
         "seed": "int|None"},
        {"summary": "dict", "curve": "dict[int,int]"},
        side_effects=False, tags=("simulation",),
    )
    def _forecast(invoices: List[Dict[str, Any]],
                  policy: Optional[Dict[str, Any]] = None,
                  n_runs: Optional[int] = 200,
                  seed: Optional[int] = 7) -> Dict[str, Any]:
        pol = SimPolicy(**policy) if policy else SimPolicy()
        res = simulate(invoices, _profiles(), policy=pol,
                       n_runs=n_runs or 200, seed=seed if seed is not None else 7)
        return {"summary": res.summary, "curve": res.curve()}


_PROFILES: Dict[str, PayerProfile] = {}


def set_profiles(profiles: Dict[str, PayerProfile]) -> None:
    _PROFILES.clear()
    _PROFILES.update(profiles)


def _profiles() -> Dict[str, PayerProfile]:
    return _PROFILES
