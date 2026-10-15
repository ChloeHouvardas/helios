"""Publish this app's verified changes to the tenant event bus.

When deployed, the engine sets `HELIOS_REDIS_URL` and the platform SDK is on the
path; a published event reaches every app in the tenant (Helios Cash Planner
re-projects on `invoice.extension_agreed` and `invoice.payment_applied`). Run by hand there is no bus,
and that is fine: the graph node is the record, the event is the notification.
Publishing never raises into a tool handler -- a bus outage must not turn a
settled payment into an error.
"""
from __future__ import annotations

import os
from typing import Any


def publish(event: str, **data: Any) -> dict[str, Any]:
    if not os.environ.get("HELIOS_REDIS_URL"):
        return {"status": "no_bus", "event": event}
    try:
        from helios_sdk import publish_event
    except ImportError:
        return {"status": "sdk_unavailable", "event": event}
    try:
        return publish_event(event, **data)
    except Exception as exc:  # noqa: BLE001 - never fail the side effect over the bus
        return {"status": "error", "event": event, "detail": str(exc)[:200]}
