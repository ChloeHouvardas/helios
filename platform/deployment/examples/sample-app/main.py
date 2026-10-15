"""Example Helios app (what a Qwen-generated app should look like).

Template contract: a FastAPI `app` object exposing GET /health, listening on
the port declared in helios.yaml (default 3000). No Dockerfile needed —
Helios injects the locked-down one at deploy time.
"""
from __future__ import annotations

import collections
import os

from fastapi import FastAPI

from helios_sdk import propose_action, publish_event, subscribe_events

APP = os.getenv("HELIOS_APP_NAME", "sample-collect")
TENANT = os.getenv("HELIOS_TENANT", "demo")

app = FastAPI(title=f"Helios app: {APP}")
EVENTS: collections.deque = collections.deque(maxlen=50)

subscribe_events(lambda e: EVENTS.append(e))


@app.get("/health")
def health():
    return {"status": "ok", "app": APP, "tenant": TENANT}


@app.get("/")
def root():
    return {
        "app": APP,
        "tenant": TENANT,
        "platform": "helios",
        "try": ["GET /health", "POST /demo/send-reminder",
                "POST /demo/collect-payment", "POST /demo/announce-event",
                "GET /events"],
    }


@app.post("/demo/send-reminder")
def send_reminder():
    """The action agent proposes an email; the sidecar decides and forwards to core."""
    return propose_action(
        "email.send",
        to="ap@northwind.example",
        subject="Invoice INV-0042 is due",
        body="Friendly reminder: INV-0042 is due on 2026-10-28. Happy to discuss terms.",
    )


@app.post("/demo/collect-payment")
def collect_payment():
    """A payment proposed WITHOUT the owner's intent auth code.

    The sidecar forwards it; the core's intent control rejects it, and the
    rejection is recorded as a decision. That is the platform refusing to move
    money nobody confirmed, not an error.
    """
    return propose_action(
        "payments.send",
        payer="operating",
        payee="Northwind Supplies",
        amount_cents=25000,
        currency="CNY",
        memo="INV-0042",
    )


@app.post("/demo/announce-event")
def announce_event():
    publish_event("receivable.rescheduled", invoice="INV-0042", new_due="2026-10-28")
    return {"status": "published", "channel": f"{TENANT}:events"}


@app.get("/events")
def events():
    return {"events": list(EVENTS)}
