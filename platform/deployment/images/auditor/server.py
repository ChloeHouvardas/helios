"""Helios Audit Agent (enforced sidecar, zero-trust gateway).

The untrusted app container only holds ACTION_PROPOSAL_TOKEN and can merely
PROPOSE an action here. This service holds the AUDIT_EXECUTION_SECRET and is
the only container allowed to talk to the Helios Financial Tools core.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

APP_NAME = os.environ.get("HELIOS_APP_NAME", "app")
TENANT = os.environ.get("HELIOS_TENANT", "demo")
EXPECTED_TOKEN = os.environ["EXPECTED_ACTION_TOKEN"]
EXECUTION_SECRET = os.environ["HELIOS_EXECUTION_SECRET"]
CORE_URL = os.environ.get("HELIOS_CORE_URL", "http://helios-core:9000")
ALLOWED = {t.strip() for t in os.environ.get("ALLOWED_TOOLS", "payments,email").split(",") if t.strip()}
MAX_PAYMENT = float(os.environ.get("MAX_PAYMENT_AMOUNT", "10000"))
DEDUPE_WINDOW_S = float(os.environ.get("DEDUPE_WINDOW_S", "120"))

app = FastAPI(title=f"helios-audit-{APP_NAME}-{TENANT}")
_seen: dict[str, float] = {}
DECISIONS: list[dict] = []


def _record(decision: str, action: str, payload: dict, reason: str = "", result=None):
    entry = {
        "audit_id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "app": APP_NAME,
        "decision": decision,
        "action": action,
        "payload": payload,
        "reason": reason,
    }
    if result is not None:
        entry["result"] = result
    DECISIONS.append(entry)
    DECISIONS[:] = DECISIONS[-500:]
    print(json.dumps(entry), flush=True)
    return entry


@app.get("/health")
def health():
    return {"status": "ok", "role": "audit-agent", "app": APP_NAME, "allowed_tools": sorted(ALLOWED)}


@app.get("/audit-log")
def audit_log():
    return {"decisions": DECISIONS[-100:]}


@app.post("/execute")
async def execute(request: Request, authorization: str = Header(default="")):
    if authorization != f"Bearer {EXPECTED_TOKEN}":
        raise HTTPException(401, "invalid action-proposal token")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(422, "body must be JSON")
    action = str(body.get("action", ""))
    payload = body.get("payload") or {}
    if not isinstance(payload, dict):
        raise HTTPException(422, "payload must be an object")

    # --- policy checks -------------------------------------------------
    if action not in ALLOWED:
        _record("held", action, payload, reason=f"tool '{action}' not granted in helios.yaml")
        return {"status": "held_for_review", "reason": f"tool '{action}' not granted"}
    if action == "payments":
        try:
            amount = float(payload.get("amount", 0))
        except (TypeError, ValueError):
            _record("held", action, payload, reason="unparseable amount")
            return {"status": "held_for_review", "reason": "unparseable amount"}
        if amount > MAX_PAYMENT:
            _record("held", action, payload, reason=f"amount {amount} exceeds cap {MAX_PAYMENT}")
            return {"status": "held_for_review",
                    "reason": f"amount {amount} exceeds per-payment cap {MAX_PAYMENT}"}
        digest = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        now = time.time()
        stale = [k for k, v in _seen.items() if now - v > DEDUPE_WINDOW_S]
        for k in stale:
            _seen.pop(k, None)
        if digest in _seen:
            _record("held", action, payload, reason="possible duplicate within dedupe window")
            return {"status": "held_for_review",
                    "reason": "possible duplicate invoice/payment within "
                              f"{DEDUPE_WINDOW_S:.0f}s window"}
        _seen[digest] = now

    # --- execute through the trusted channel ----------------------------
    try:
        resp = httpx.post(f"{CORE_URL}/tools/{action}", json=payload,
                          headers={"Authorization": f"Bearer {EXECUTION_SECRET}"},
                          timeout=10)
        result = resp.json() if "json" in resp.headers.get("content-type", "") else resp.text
    except Exception as exc:
        _record("error", action, payload, reason=f"core unreachable: {exc}")
        raise HTTPException(502, f"helios core unreachable: {exc}")

    entry = _record("executed", action, payload, result=result)
    return {"status": "executed", "audit_id": entry["audit_id"], "result": result}
