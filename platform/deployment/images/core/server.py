"""Helios Financial Tools Developer Interface (mock for local dev).

Only reachable from inside the tenant network, and only by holders of the
tenant execution secret (given to Audit Agents, never to app containers).
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid

from fastapi import FastAPI, Header, HTTPException, Request

SECRET = os.environ["HELIOS_EXECUTION_SECRET"]
DB_PATH = os.environ.get("HELIOS_DB", "/tmp/helios_graph.db")

app = FastAPI(title="helios-core")


def _db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS tool_calls "
                 "(ref TEXT PRIMARY KEY, ts REAL, tool TEXT, payload TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS kg_nodes "
                 "(entity TEXT PRIMARY KEY, props TEXT, updated REAL)")
    conn.commit()
    return conn


def _require_auth(authorization: str):
    if authorization != f"Bearer {SECRET}":
        raise HTTPException(401, "missing/invalid execution secret")


@app.get("/health")
def health():
    return {"status": "ok", "role": "helios-core"}


@app.post("/tools/{tool}")
async def tool(tool: str, request: Request, authorization: str = Header(default="")):
    _require_auth(authorization)
    payload = await request.json()
    ref = uuid.uuid4().hex[:12]
    if tool == "payments":
        result = {"status": "simulated_sent", "ref": ref,
                  "to": payload.get("to"), "amount": payload.get("amount")}
    elif tool == "email":
        result = {"status": "simulated_sent", "ref": ref, "to": payload.get("to"),
                  "subject": payload.get("subject")}
    elif tool == "knowledge-graph":
        entity = str(payload.get("entity", "")) or ref
        _db().execute(
            "INSERT INTO kg_nodes(entity, props, updated) VALUES (?,?,?) "
            "ON CONFLICT(entity) DO UPDATE SET props=excluded.props, updated=excluded.updated",
            (entity, str(payload.get("props", {})), time.time()),
        )
        _db().commit()
        result = {"status": "upserted", "entity": entity}
    else:
        raise HTTPException(404, f"unknown tool: {tool}")
    _db().execute("INSERT INTO tool_calls VALUES (?,?,?,?)",
                  (ref, time.time(), tool, str(payload)))
    _db().commit()
    return {"tool": tool, **result}


@app.get("/kg/nodes")
def kg_nodes(authorization: str = Header(default="")):
    _require_auth(authorization)
    rows = _db().execute("SELECT entity, props, updated FROM kg_nodes").fetchall()
    return {"nodes": [{"entity": r[0], "props": r[1], "updated": r[2]} for r in rows]}
