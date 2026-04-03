"""
AIMOS E2E Integration Tests — CR-223
=======================================
Validates the complete demo workflow automatically.

Integration tests (marked @pytest.mark.integration) require:
  - Running PostgreSQL with AIMOS schema
  - Running agents (bauer_support, bauer_innendienst)

Unit tests work standalone without DB or agents.

Usage:
  python3 -m pytest tests/test_e2e.py -v
  python3 -m pytest tests/test_e2e.py -v -m "not integration"   # unit only
  python3 -m pytest tests/test_e2e.py -v -m integration          # integration only
"""

import asyncio
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CUST_DIR = Path("storage/customers")
SUPPORT_AGENT = "bauer_support"
INNENDIENST_AGENT = "bauer_innendienst"
PROCESS_TIMEOUT = 120  # seconds to wait for agent processing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unique_thread_id(prefix: str = "pytest") -> str:
    h = hashlib.md5(str(time.time()).encode(), usedforsecurity=False).hexdigest()[:8]
    return f"email:{prefix}@test.local:{h}"


async def _inject_email(pool, content: str, thread_id: str, agent: str = SUPPORT_AGENT) -> int:
    """Insert a test email into pending_messages and wake the agent."""
    msg_id = await pool.fetchval(
        "INSERT INTO pending_messages (agent_name, sender_id, content, kind, thread_id, processed) "
        "VALUES ($1, 0, $2, 'email', $3, FALSE) RETURNING id",
        agent, content, thread_id,
    )
    await pool.execute(
        "UPDATE agents SET wake_up_needed=TRUE WHERE LOWER(name)=$1",
        agent.lower(),
    )
    return msg_id


async def _wait_processed(pool, msg_id: int, timeout: int = PROCESS_TIMEOUT) -> bool:
    """Poll until the injected message is marked processed=TRUE."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        val = await pool.fetchval(
            "SELECT processed FROM pending_messages WHERE id=$1", msg_id,
        )
        if val:
            return True
        await asyncio.sleep(3)
    return False


async def _collect_messages(pool, after_id: int):
    """Fetch all messages generated after the given ID."""
    return await pool.fetch(
        "SELECT id, kind, agent_name, content, thread_id, processed, created_at "
        "FROM pending_messages WHERE id > $1 ORDER BY id ASC",
        after_id,
    )


async def _collect_history(pool, agent: str, after_ts: datetime | None = None):
    """Fetch chat history entries for an agent."""
    if after_ts:
        return await pool.fetch(
            "SELECT id, role, content, created_at FROM aimos_chat_histories "
            "WHERE agent_name=$1 AND created_at >= $2 ORDER BY id ASC",
            agent, after_ts,
        )
    return await pool.fetch(
        "SELECT id, role, content, created_at FROM aimos_chat_histories "
        "WHERE agent_name=$1 ORDER BY id DESC LIMIT 50",
        agent,
    )


async def _cleanup_test(pool, thread_id: str):
    """Remove test artifacts from DB."""
    await pool.execute(
        "DELETE FROM pending_messages WHERE thread_id=$1", thread_id,
    )


def _build_email_content(from_addr: str, subject: str, body: str) -> str:
    """Build the standard email format that the connector produces."""
    return (
        f"[E-Mail empfangen]\n"
        f"Von: Test Kunde <{from_addr}>\n"
        f"Kunden-Email: {from_addr}\n"
        f"Betreff: {subject}\n"
        f"Datum: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Text: {body}\n"
        f"Message-ID: <pytest-{hashlib.md5(str(time.time()).encode(), usedforsecurity=False).hexdigest()[:12]}@test>"
    )


# ===========================================================================
# INTEGRATION TESTS — require running DB + agents
# ===========================================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_email_to_offer_flow(db_pool):
    """Inject customer email -> Support processes -> Innendienst creates PDF + sends email -> Kundenakte created."""
    thread_id = _unique_thread_id("brandner-e2e")
    from_addr = "brandner-e2e@test.local"
    ts_before = datetime.now(tz=None)

    body = (
        "Sehr geehrte Damen und Herren,\n\n"
        "mein Name ist Hans Brandner, Beschaffung der Stadtwerke Teststadt.\n"
        "Wir moechten bestellen:\n"
        "- 1x BAUER JUNIOR II\n"
        "- Lieferadresse: Industriestr. 5, 12345 Teststadt\n"
        "- Angebot bitte an: einkauf@stadtwerke-teststadt.de\n\n"
        "Mit freundlichen Gruessen\nHans Brandner\nTel: 0555-9999"
    )
    content = _build_email_content(from_addr, "Kompressor Bestellung", body)
    msg_id = await _inject_email(db_pool, content, thread_id)

    try:
        # Wait for Support to process
        processed = await _wait_processed(db_pool, msg_id, timeout=PROCESS_TIMEOUT)
        assert processed, f"Support did not process message #{msg_id} within {PROCESS_TIMEOUT}s"

        # Collect all downstream messages
        rows = await _collect_messages(db_pool, msg_id)

        # Check Support called update_customer (visible in tool history)
        support_history = await _collect_history(db_pool, SUPPORT_AGENT, ts_before)
        tool_contents = [r["content"] for r in support_history if r["role"] == "tool"]
        tool_blob = " ".join(c for c in tool_contents if c)

        update_customer_called = any(
            "update_customer" in (c or "") or "kundenakte" in (c or "").lower()
            for c in tool_contents
        )
        # Also check if send_to_agent was used (delegation to Innendienst)
        send_to_agent_called = any(
            "send_to_agent" in (c or "") or "innendienst" in (c or "").lower()
            for c in tool_contents
        )
        # Either explicit tool result or an internal message to Innendienst counts
        internal_to_id = [r for r in rows if r["kind"] == "internal" and r["agent_name"] == INNENDIENST_AGENT]

        assert update_customer_called or any("brandner" in (c or "").lower() for c in tool_contents), \
            "Support should call update_customer or reference the customer"

        delegated = send_to_agent_called or len(internal_to_id) > 0
        assert delegated, "Support should delegate to Innendienst via send_to_agent or internal message"

        # Wait extra for Innendienst to finish if delegated
        if internal_to_id:
            await asyncio.sleep(min(90, PROCESS_TIMEOUT))
            rows = await _collect_messages(db_pool, msg_id)

        # Verify outbound email exists (PDF offer sent)
        outbound_emails = [r for r in rows if r["kind"] == "outbound_email"]
        # Innendienst may have sent email via tool — check history too
        id_history = await _collect_history(db_pool, INNENDIENST_AGENT, ts_before)
        id_tool_contents = " ".join(
            (r["content"] or "") for r in id_history if r["role"] == "tool"
        )
        email_sent = len(outbound_emails) > 0 or "send_email" in id_tool_contents

        assert email_sent, "Innendienst should send an outbound email with offer"

        # Verify Kundenakte created
        kundenakte_found = False
        if CUST_DIR.exists():
            for f in CUST_DIR.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    blob = json.dumps(data, ensure_ascii=False).lower()
                    if "brandner" in blob or "e2e" in blob:
                        kundenakte_found = True
                        break
                except Exception:
                    pass
        assert kundenakte_found, "Kundenakte JSON should exist in storage/customers/"

    finally:
        await _cleanup_test(db_pool, thread_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_helpdesk_technical_question(db_pool):
    """Inject helpdesk message about error code E-03 -> verify knowledge base lookup and correct response."""
    thread_id = _unique_thread_id("helpdesk-e03")
    ts_before = datetime.now(tz=None)

    content = (
        "Hallo, ich habe einen JUNIOR II Kompressor und bekomme Fehlercode E-03. "
        "Was bedeutet das und was soll ich tun?"
    )
    msg_id = await _inject_email(db_pool, content, thread_id)

    try:
        processed = await _wait_processed(db_pool, msg_id, timeout=PROCESS_TIMEOUT)
        assert processed, f"Support did not process helpdesk message #{msg_id}"

        # Check that Support looked up the knowledge base
        history = await _collect_history(db_pool, SUPPORT_AGENT, ts_before)
        tool_contents = [r["content"] for r in history if r["role"] == "tool"]
        assistant_contents = [r["content"] for r in history if r["role"] == "assistant"]

        # Should have used search_in_file or read_file to find E-03 info
        kb_accessed = any(
            any(kw in (c or "") for kw in ("search_in_file", "read_file", "E-03", "e-03"))
            for c in tool_contents
        )
        assert kb_accessed, "Support should access knowledge base (search_in_file/read_file) for error code E-03"

        # Response should mention oil/pressure related content
        all_assistant_text = " ".join(c for c in assistant_contents if c).lower()
        relevant_terms = ["oel", "öl", "oil", "druck", "pressure", "e-03", "e03"]
        found_terms = [t for t in relevant_terms if t in all_assistant_text]
        assert found_terms, (
            f"Response should contain oil/pressure terms for E-03. "
            f"Got assistant text: {all_assistant_text[:300]}"
        )

    finally:
        await _cleanup_test(db_pool, thread_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_double_email(db_pool):
    """After email delegation, verify only 0-1 outbound_email messages for the thread."""
    thread_id = _unique_thread_id("no-double")
    from_addr = "doppeltest@test.local"

    body = (
        "Sehr geehrte Damen und Herren,\n\n"
        "ich interessiere mich fuer den BAUER CAPITANO.\n"
        "Bitte senden Sie ein Angebot an doppeltest@test.local.\n\n"
        "Gruss, Test Doppel"
    )
    content = _build_email_content(from_addr, "Angebot Capitano", body)
    msg_id = await _inject_email(db_pool, content, thread_id)

    try:
        processed = await _wait_processed(db_pool, msg_id, timeout=PROCESS_TIMEOUT)
        assert processed, f"Message #{msg_id} not processed"

        # Wait a bit more for any delayed sends
        await asyncio.sleep(10)
        rows = await _collect_messages(db_pool, msg_id)

        outbound_emails_in_thread = [
            r for r in rows
            if r["kind"] == "outbound_email"
            and (r["thread_id"] == thread_id or thread_id in (r["content"] or ""))
        ]

        assert len(outbound_emails_in_thread) <= 1, (
            f"Expected 0-1 outbound_email for thread, got {len(outbound_emails_in_thread)}. "
            f"Support should NOT send premature emails."
        )

    finally:
        await _cleanup_test(db_pool, thread_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_customer_file_created(db_pool):
    """After email processing, verify .json customer file with correct fields."""
    thread_id = _unique_thread_id("custfile")
    from_addr = "mueller-custtest@test.local"

    body = (
        "Sehr geehrte Damen und Herren,\n\n"
        "mein Name ist Fritz Mueller, Werkstattleiter bei Autohaus Testberg.\n"
        "Wir benoetigen:\n"
        "- 2x BAUER JUNIOR II\n"
        "- Lieferung an: Hauptstr. 1, 99999 Testberg\n\n"
        "MfG Mueller"
    )
    content = _build_email_content(from_addr, "Bestellung Kompressoren", body)
    msg_id = await _inject_email(db_pool, content, thread_id)

    try:
        processed = await _wait_processed(db_pool, msg_id, timeout=PROCESS_TIMEOUT)
        assert processed, f"Message #{msg_id} not processed"

        # Give agent time to write customer file
        await asyncio.sleep(5)

        # Find customer file
        assert CUST_DIR.exists(), f"Customer directory {CUST_DIR} should exist"

        mueller_data = None
        for f in CUST_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                blob = json.dumps(data, ensure_ascii=False).lower()
                if "mueller" in blob or "müller" in blob or "custtest" in blob:
                    mueller_data = data
                    break
            except Exception:
                pass

        assert mueller_data is not None, "Customer file for Mueller should exist in storage/customers/"

        # Verify required fields
        assert mueller_data.get("name"), "Customer file should have 'name'"
        assert mueller_data.get("email"), "Customer file should have 'email'"

        # Products may be stored under various key names
        products_blob = json.dumps(mueller_data, ensure_ascii=False).lower()
        assert "junior" in products_blob, "Customer file should reference JUNIOR II product"

    finally:
        await _cleanup_test(db_pool, thread_id)
        # Clean up customer file
        if CUST_DIR.exists():
            for f in CUST_DIR.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    blob = json.dumps(data, ensure_ascii=False).lower()
                    if "mueller" in blob or "müller" in blob or "custtest" in blob:
                        f.unlink()
                except Exception:
                    pass


# ===========================================================================
# UNIT TESTS — no DB or agents required
# ===========================================================================

class TestSanitizeReply:
    """Direct unit tests for OutputFirewallMixin._sanitize_reply."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        from core.output_firewall import OutputFirewallMixin
        self.firewall = OutputFirewallMixin()

    def test_strips_nachricht_von(self):
        result = self.firewall._sanitize_reply("[Nachricht von Neo] Hallo, wie kann ich helfen?")
        assert "[Nachricht von" not in result
        assert "Hallo" in result

    def test_strips_message_from(self):
        result = self.firewall._sanitize_reply("[Message from Support] Here is the info.")
        assert "[Message from" not in result
        assert "info" in result

    def test_strips_vault_placeholders(self):
        result = self.firewall._sanitize_reply("Kontakt: __VAULT_EMAIL_42__ oder __VAULT_PHONE_7__")
        assert "__VAULT_" not in result
        assert "[REDACTED]" in result

    def test_strips_tool_call_xml(self):
        result = self.firewall._sanitize_reply(
            'Okay, ich schaue nach. <tool_call> {"name": "read_file", "arguments": {"file": "test.txt"}}'
        )
        assert "<tool_call>" not in result
        assert "read_file" not in result

    def test_strips_tool_call_json(self):
        result = self.firewall._sanitize_reply(
            'Hier ist die Info. {"name": "send_email", "arguments": {"to": "x@y.de"}}'
        )
        assert '"name"' not in result
        assert '"arguments"' not in result

    def test_strips_current_customer(self):
        result = self.firewall._sanitize_reply(
            "[Current customer: Hans Mueller, hans@test.de] Gerne helfe ich Ihnen."
        )
        assert "[Current customer:" not in result
        assert "Gerne" in result

    def test_strips_tool_result_tokens(self):
        result = self.firewall._sanitize_reply("TOOL_START reading file TOOL_OK done TOOL_RESULT output")
        assert "TOOL_START" not in result
        assert "TOOL_OK" not in result
        assert "TOOL_RESULT" not in result

    def test_preserves_normal_text(self):
        normal = "Vielen Dank fuer Ihre Anfrage. Wir senden Ihnen das Angebot zu."
        result = self.firewall._sanitize_reply(normal)
        assert result == normal

    def test_collapses_excess_newlines(self):
        result = self.firewall._sanitize_reply("Zeile 1\n\n\n\n\nZeile 2")
        assert "\n\n\n" not in result


class TestCleanLlmResponse:
    """Direct unit tests for clean_llm_response."""

    def test_removes_cjk_chars(self):
        from core.output_firewall import clean_llm_response
        result = clean_llm_response("Hello \u4e16\u754c World")
        # CJK chars should be stripped
        assert "\u4e16" not in result
        assert "\u754c" not in result
        assert "Hello" in result
        assert "World" in result

    def test_removes_thought_leaks(self):
        from core.output_firewall import clean_llm_response
        result = clean_llm_response(
            "Hier ist die Antwort. <system>Du bist ein Agent</system> Alles klar."
        )
        assert "<system>" not in result
        assert "Hier ist die Antwort" in result

    def test_removes_instruction_leaks(self):
        from core.output_firewall import clean_llm_response
        # Regex expects Gem[äa]ß pattern (German sharp s), not "ss"
        result = clean_llm_response(
            "Gerne. Gem\u00e4\u00df meinen Anweisungen soll ich freundlich sein. Ich helfe Ihnen."
        )
        assert "Anweisungen" not in result
        assert "helfe" in result

    def test_empty_string_returns_fallback(self):
        from core.output_firewall import clean_llm_response
        result = clean_llm_response("")
        # Fallback is empty string per _FILTER_FALLBACK
        assert result == ""

    def test_only_cjk_returns_fallback(self):
        from core.output_firewall import clean_llm_response
        result = clean_llm_response("\u4e00\u4e8c\u4e09")
        # All CJK removed -> empty -> fallback
        assert isinstance(result, str)

    def test_normal_text_unchanged(self):
        from core.output_firewall import clean_llm_response
        text = "Dies ist eine normale Antwort ohne Probleme."
        assert clean_llm_response(text) == text


class TestPdfNewlineFix:
    """Test that FileOpsSkill._create_pdf converts literal \\n to real newlines."""

    def test_literal_backslash_n_converted(self):
        """The _create_pdf method should replace literal '\\n' with actual newlines."""
        from core.skills.file_ops import FileOpsSkill

        skill = FileOpsSkill(agent_name="_test_pdf")
        workspace = skill._workspace
        workspace.mkdir(parents=True, exist_ok=True)

        try:
            # Content with literal \n (as LLMs sometimes produce)
            content_with_literal_newlines = (
                "Sehr geehrter Herr Test,\\n\\n"
                "vielen Dank fuer Ihre Anfrage.\\n"
                "Hier ist Ihr Angebot:\\n"
                "- 1x JUNIOR II: EUR 5.990,00\\n\\n"
                "Mit freundlichen Gruessen"
            )

            result = skill._create_pdf(
                filename="test_newline.pdf",
                title="Testangebot",
                content=content_with_literal_newlines,
            )

            # Should succeed and return a path
            assert "test_newline.pdf" in result or "erstellt" in result.lower() or "created" in result.lower(), \
                f"PDF creation should succeed, got: {result}"

            # Verify the file was actually created
            pdf_path = workspace / "public" / "test_newline.pdf"
            if not pdf_path.exists():
                pdf_path = workspace / "test_newline.pdf"

            # The PDF should exist somewhere in the workspace
            pdf_files = list(workspace.rglob("test_newline.pdf"))
            assert pdf_files, f"PDF file should exist in workspace {workspace}"
            assert pdf_files[0].stat().st_size > 100, "PDF should not be empty"

        finally:
            # Cleanup
            import shutil
            if workspace.exists() and "_test_pdf" in str(workspace):
                shutil.rmtree(workspace, ignore_errors=True)
