"""Configuration and thresholds for Helios Cash Planner.

Every number the audit manifest declares appears here too. The manifest is the
authority — the auditor compiles `agents/helios-cash-planner-audit.yaml` and
nothing else — but a threshold that lives only in YAML is invisible to the page
that has to explain it. Mirroring it here means the dashboard, the README and
the control tests quote the figure the gate actually enforces.

Money is integer cents internally and decimal strings at the edges, never float.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

APP_ID = "helios-cash-planner"
APP_NAME = "Helios Cash Planner"

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
AGENTS_DIR = HERE / "agents"
TEMPLATES_DIR = HERE / "templates"

AUDIT_MANIFEST = AGENTS_DIR / f"{APP_ID}-audit.yaml"
ACTION_MANIFEST = AGENTS_DIR / f"{APP_ID}-action.json"

ACCOUNTS_FILE = DATA_DIR / "accounts.json"
CUSTOMERS_FILE = DATA_DIR / "customers.json"
RECEIVABLES_FILE = DATA_DIR / "receivables.json"
PAYABLES_FILE = DATA_DIR / "payables.json"

OPERATING_ACCOUNT = "operating"
ACCOUNT_CURRENCY = "CNY"

# --- task types ---------------------------------------------------------------
TASK_PLAN = "cashplan.plan"          # project, warn, recommend
TASK_DIGEST = "cashplan.digest"      # the weekly owner digest

# --- the forecast ---------------------------------------------------------------
#: Thirteen weeks, in days. The horizon every projection and every week id uses.
HORIZON_DAYS = 91
WEEKS = HORIZON_DAYS // 7

#: A week is SHORT when its projected minimum balance falls below this buffer.
#: 5,000.00 CNY: enough to absorb a day of timing noise, not enough to hide a
#: real gap. Manifest: `app.warning_only_when_short`, via the fact `week_not_short`.
CASH_BUFFER_CENTS = 500_000

#: Monte Carlo settings for `simulate_cash`. Seeded, so the same inputs give the
#: same probability every time.
SIMULATION_PATHS = 1000
SIMULATION_SEED = 7

#: Credit facility. A draw recommendation is capped against the remaining
#: headroom as a FACT, so the agent cannot recommend more than exists.
CREDIT_LINE_LIMIT_CENTS = 15_000_000
CREDIT_LINE_SPREAD_BPS = 180
CREDIT_LINE_BASE_SERIES = "lpr_1y"

#: The owner gets at most one digest per rolling window.
#: Manifest: `app.one_digest_per_week`.
MAX_DIGESTS = 1
DIGEST_WINDOW_DAYS = 7
OWNER_EMAIL = "owner@helios-cash-planner.local"

#: How many customers a follow-up recommendation may name at once: one. A
#: recommendation is one action for one week.
FOLLOW_UP_MIN_REASON_CHARS = 12

# --- graph identity ------------------------------------------------------------
GRAPH_SOURCE = "helios-cash-planner"
GRAPH_OBSERVER = "helios-cash-planner-agent"
EVENT_CHANNEL_EVENTS = ("invoice.extension_agreed", "invoice.payment_applied",
                        "bill.rescheduled", "bill.paid")


def cents_to_decimal(cents: int | None) -> Decimal:
    return Decimal(int(cents or 0)) / Decimal(100)


def money(cents: int | None, currency: str = ACCOUNT_CURRENCY) -> str:
    return f"{cents_to_decimal(cents):,.2f} {currency}"


def describe() -> dict[str, Any]:
    return {
        "app_id": APP_ID,
        "operating_account": OPERATING_ACCOUNT,
        "account_currency": ACCOUNT_CURRENCY,
        "horizon_days": HORIZON_DAYS,
        "weeks": WEEKS,
        "cash_buffer": money(CASH_BUFFER_CENTS),
        "credit_line_limit": money(CREDIT_LINE_LIMIT_CENTS),
        "credit_line_spread_bps": CREDIT_LINE_SPREAD_BPS,
        "credit_line_base_series": CREDIT_LINE_BASE_SERIES,
        "max_digests": MAX_DIGESTS,
        "digest_window_days": DIGEST_WINDOW_DAYS,
        "simulation_paths": SIMULATION_PATHS,
    }
