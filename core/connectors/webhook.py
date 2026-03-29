"""
AIMOS Webhook Connector
========================
Receives HTTP POST requests from external systems and creates
pending_messages for agents.

Endpoint: POST /api/webhook/{agent_name}
Auth: X-Webhook-Token header (per-agent, stored in env_secrets as WEBHOOK_TOKEN)
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.responses import JSONResponse

from core.db_pool import db_connection

log = logging.getLogger("AIMOS.webhook")
webhook_router = APIRouter(tags=["webhook"])


def _get_webhook_token(agent_name: str) -> str | None:
    """Fetch the WEBHOOK_TOKEN from the agent's env_secrets."""
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT env_secrets FROM agents WHERE name=%s", (agent_name,))
                row = cur.fetchone()
        if not row:
            return None
        secrets = row.get("env_secrets") or {}
        if isinstance(secrets, str):
            secrets = json.loads(secrets)
        return secrets.get("WEBHOOK_TOKEN")
    except Exception:
        return None


@webhook_router.post("/api/webhook/{agent_name}", response_class=JSONResponse)
async def webhook_receive(agent_name: str, request: Request,
                          x_webhook_token: str = Header(...)):
    """Receive a webhook POST and queue it as a pending_message."""
    name = agent_name.lower()

    # Verify token
    expected_token = _get_webhook_token(name)
    if not expected_token:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found or no WEBHOOK_TOKEN configured")
    if x_webhook_token != expected_token:
        raise HTTPException(status_code=403, detail="Invalid webhook token")

    # Parse body
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    text = body.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="'text' field required")

    source = body.get("source", "webhook")

    # Insert pending_message
    try:
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pending_messages (agent_name, kind, sender_id, content, processed) "
                    "VALUES (%s, 'webhook', 0, %s, FALSE) RETURNING id",
                    (name, json.dumps({"text": text, "source": source})),
                )
                msg_id = cur.fetchone()["id"]
            conn.commit()
        log.info(f"Webhook queued msg_id={msg_id} for agent={name} source={source}")
        return {"status": "queued", "agent": name, "msg_id": msg_id}
    except Exception as exc:
        log.error(f"Webhook DB error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
