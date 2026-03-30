"""
AIMOS REST API Connector
=========================
External software can interact with agents via REST API.

Endpoints:
  POST /api/v1/agents/{agent_name}/ask       — Send a question (queued)
  GET  /api/v1/agents/{agent_name}/response/{msg_id} — Poll for response

Auth: Dashboard HTTP Basic Auth (applied globally via app dependencies).
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from core.db_pool import db_connection

log = logging.getLogger("AIMOS.rest_api")
rest_api_router = APIRouter(tags=["rest_api"])


@rest_api_router.post("/api/v1/agents/{agent_name}/ask", response_class=JSONResponse)
async def rest_api_ask(agent_name: str, request: Request):
    """Queue a question for an agent. Returns msg_id for polling."""
    name = agent_name.lower()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    text = body.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="'text' field required")

    callback_url = body.get("callback_url", "")

    # Verify agent exists
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM agents WHERE name=%s", (name,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

                # Insert pending_message — use kind/sender_id/thread_id from body if provided
                _kind = body.get("kind", "dashboard")
                _sender_id = int(body.get("sender_id", 0))
                _thread_id = body.get("thread_id", "")
                cur.execute(
                    "INSERT INTO pending_messages (agent_name, kind, sender_id, content, processed, thread_id) "
                    "VALUES (%s, %s, %s, %s, FALSE, %s) RETURNING id",
                    (name, _kind, _sender_id, text, _thread_id),
                )
                msg_id = cur.fetchone()["id"]
            conn.commit()
        log.info(f"REST API queued msg_id={msg_id} for agent={name}")
        return {"status": "queued", "msg_id": msg_id}
    except HTTPException:
        raise
    except Exception as exc:
        log.error(f"REST API DB error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@rest_api_router.get("/api/v1/agents/{agent_name}/response/{msg_id}", response_class=JSONResponse)
async def rest_api_response(agent_name: str, msg_id: int):
    """Poll for the response to a queued message."""
    name = agent_name.lower()

    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                # Check if the request has been processed
                cur.execute(
                    "SELECT processed, created_at FROM pending_messages "
                    "WHERE id=%s AND agent_name=%s",
                    (msg_id, name),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail=f"Message {msg_id} not found for agent '{name}'")

                if not row["processed"]:
                    return {"status": "pending"}

                # Message was processed — find the agent's FINAL reply in chat history
                # Skip tool-call intermediates: look for the last assistant message
                # that is NOT a tool call (JSON tool invocation or XML function_call)
                cur.execute(
                    "SELECT content FROM aimos_chat_histories "
                    "WHERE agent_name=%s AND role='assistant' AND created_at >= %s "
                    "ORDER BY id DESC LIMIT 10",
                    (name, row["created_at"]),
                )
                candidate_rows = cur.fetchall()

        # Find the first (most recent) non-tool-call response
        reply_content = None
        for cand in (candidate_rows if candidate_rows else []):
            c = (cand["content"] or "").strip()
            # Skip XML-style tool calls
            if "<function_call>" in c or "```xml\n<function_call>" in c:
                continue
            # Skip JSON-style tool calls (Qwen format)
            if c.startswith("[Tool:") or c.startswith('{"'):
                continue
            # Skip very short non-answers
            if len(c) < 10:
                continue
            reply_content = c
            break

        if reply_content:
            return {"status": "complete", "response": reply_content}
        else:
            # Race condition: message marked processed but history not yet written
            return {"status": "pending"}
    except HTTPException:
        raise
    except Exception as exc:
        log.error(f"REST API poll error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@rest_api_router.get("/api/v1/helpdesk/replies", response_class=JSONResponse)
async def rest_api_helpdesk_replies(since_id: int = 0):
    """Poll outbound_telegram messages for the demo helpdesk (sender_id=9999999)."""
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, agent_name, content FROM pending_messages "
                    "WHERE kind='outbound_telegram' AND sender_id=9999999 AND id > %s "
                    "ORDER BY id ASC LIMIT 10",
                    (since_id,),
                )
                rows = cur.fetchall()
                return {"replies": [{"id": r["id"], "agent": r["agent_name"], "text": r["content"]} for r in rows]}
    except Exception as exc:
        log.error(f"Helpdesk poll error: {exc}")
        return {"replies": []}


@rest_api_router.get("/api/v1/customers/search", response_class=JSONResponse)
async def rest_api_customer_search(q: str = ""):
    """Search customer files for /k functionality in demo board."""
    import re as _re
    from pathlib import Path

    query = q.strip().lower()
    if not query or len(query) < 2:
        return {"results": []}

    cust_dir = Path("storage/customers")
    if not cust_dir.exists():
        return {"results": []}

    results = []
    for f in cust_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            searchable = (
                f.stem.lower() + " " +
                data.get("name", "").lower() + " " +
                data.get("company", "").lower() + " " +
                data.get("email", "").lower()
            )
            if query in searchable:
                results.append({
                    "name": data.get("name", f.stem),
                    "company": data.get("company", ""),
                    "email": data.get("email", ""),
                    "products": data.get("products", []),
                    "orders": data.get("orders", []),
                    "thread_id": data.get("thread_ids", [""])[0],
                    "file": f.name,
                })
        except Exception:
            pass
    return {"results": results}
