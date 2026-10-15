"""Helios App Builder SDK — pre-installed in the golden base image.

Generated apps use two primitives:
  propose_action(tool, **args) -> proposes a named platform tool call
                                  (`payments.send`, `email.send`, `graph.publish_verified`)
                                  to the app's audit sidecar, which adjudicates and
                                  forwards to the tenant core. Money is integer cents;
                                  a payment needs the owner's intent `auth_code`.
  publish_event(...)           -> emits on the tenant Redis event bus
App containers never hold execution secrets; only the proposal leaves.
"""
from __future__ import annotations

import json
import os
import threading

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

TENANT = os.getenv("HELIOS_TENANT", "demo")
APP_NAME = os.getenv("HELIOS_APP_NAME", "app")
AUDIT_URL = os.getenv("HELIOS_AUDIT_URL", "")
ACTION_TOKEN = os.getenv("HELIOS_ACTION_TOKEN", "")
REDIS_URL = os.getenv("HELIOS_REDIS_URL", "redis://helios-redis:6379/0")
EVENT_CHANNEL = f"{TENANT}:events"


def propose_action(action: str, **payload) -> dict:
    """Propose a named tool call (`payments.send`, ...) to the audit sidecar.

    Never executes directly. The sidecar denies, forwards to the tenant core,
    or reports the core's own rejection; every outcome is in the audit chain.
    """
    if httpx is None or not AUDIT_URL:
        return {"status": "unavailable", "detail": "HELIOS_AUDIT_URL not configured"}
    try:
        resp = httpx.post(
            AUDIT_URL,
            json={"action": action, "payload": payload},
            headers={"Authorization": f"Bearer {ACTION_TOKEN}"},
            timeout=15,
        )
        body = resp.json() if "json" in resp.headers.get("content-type", "") else resp.text
        return {"http_status": resp.status_code, "response": body}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


def publish_event(event: str, **data) -> dict:
    import redis

    r = redis.from_url(REDIS_URL)
    r.publish(EVENT_CHANNEL, json.dumps({"event": event, "data": data, "app": APP_NAME}))
    return {"status": "published", "channel": EVENT_CHANNEL}


def subscribe_events(handler):
    """Daemon thread: call handler(dict) for every tenant event. Never raises."""

    def loop():
        try:
            import redis

            r = redis.from_url(REDIS_URL)
            ps = r.pubsub()
            ps.subscribe(EVENT_CHANNEL)
            for msg in ps.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    handler(json.loads(msg["data"]))
                except Exception:
                    pass
        except Exception:
            pass

    t = threading.Thread(target=loop, daemon=True, name="helios-events")
    t.start()
    return t
