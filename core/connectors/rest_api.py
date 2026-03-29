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

                # Insert pending_message with plain text content (not JSON)
                cur.execute(
                    "INSERT INTO pending_messages (agent_name, kind, sender_id, content, processed) "
                    "VALUES (%s, 'dashboard', 0, %s, FALSE) RETURNING id",
                    (name, text),
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
