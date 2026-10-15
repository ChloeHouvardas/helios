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
        "try": ["GET /health", "POST /demo/collect-payment",
                "POST /demo/announce-event", "GET /events"],
    }


@app.post("/demo/collect-payment")
def collect_payment():
    """Action Agent proposes a payment; the Audit Agent decides."""
    return propose_action(
        "payments",
        to="Northwind Supplies",
        amount=250.0,
        invoice="INV-0042",
        note="friendly reminder, happy to discuss terms",
    )


@app.post("/demo/announce-event")
def announce_event():
    publish_event("receivable.rescheduled", invoice="INV-0042", new_due="2026-10-28")
    return {"status": "published", "channel": f"{TENANT}:events"}


@app.get("/events")
def events():
    return {"events": list(EVENTS)}
