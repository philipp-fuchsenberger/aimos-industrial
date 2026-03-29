# AIMOS — GDPR / DSGVO Compliance Documentation

**System:** AIMOS v4.3.6 (Artificial Intelligence Managed Operating System)
**Date:** 2026-03-22
**Author:** Philipp Fuchsenberger
**Classification:** Internal / Customer-Facing Compliance Reference

---

## 1. Architecture Overview

AIMOS is a **local-first** AI agent system. All core processing runs on-premise:

| Component | Location | Data Residency |
|---|---|---|
| LLM Inference (Qwen 3.5:27b) | Local GPU (RTX 3090) | On-premise, no external calls |
| PostgreSQL Database | Local PostgreSQL (localhost) | On-premise |
| Agent Memory (SQLite) | Local filesystem per agent | On-premise |
| Telegram Relay | Telegram Bot API | Messages transit Telegram servers |
| Email (IMAP/SMTP) | Configured mail provider | Standard email transport |
| **External LLM (optional)** | Anthropic API / OpenRouter | **Only on explicit escalation** |

**Key principle:** The system operates fully autonomous without any external AI API calls.
External LLMs are only contacted when an agent explicitly determines that a task exceeds
the capabilities of the local model.

---

## 2. External API Usage — When and Why

### 2.1 Trigger Mechanism

External API calls are **never automatic**. They occur exclusively when:

1. An agent calls the `ask_external` tool during a conversation
2. This tool is only invoked when the local LLM (Qwen 3.5:27b) determines that a question
   requires more advanced reasoning (complex analysis, legal/financial questions, code generation)

**There is no:**
- Heartbeat or health-check traffic to external APIs
- Background polling or scheduled API calls
- Automatic fallback to external LLMs on local model errors
- Telemetry, analytics, or usage reporting to any third party

### 2.2 Cost Implications

Since calls are on-demand only, costs are minimal and directly proportional to actual usage.
Typical deployment: 0-5 external calls per day, depending on task complexity.

---

## 3. PII Vault — Data Anonymization Before External Transmission

### 3.1 Architecture

Every outbound request to an external API passes through the **PII Vault** — a mandatory
anonymization layer that cannot be bypassed.

```
Agent generates prompt
    ↓
┌─────────────────────────────────────────────┐
│  PII Vault (Anonymizer)                      │
│                                              │
│  1. Scan for credentials (API keys, tokens)  │
│  2. Scan for email addresses                 │
│  3. Scan for phone numbers (+49...)          │
│  4. Scan for name patterns (Herr/Frau ...)   │
│  5. Replace ALL matches with placeholders    │
│     e.g. "philipp@example.de" → __VAULT_EMAIL_1__  │
│  6. Store mapping in local SQLite            │
│     (never leaves the server)                │
└──────────────────────┬──────────────────────┘
                       ↓
          Anonymized prompt → External API
                       ↓
          Response from External API
                       ↓
┌─────────────────────────────────────────────┐
│  PII Vault (Deanonymizer)                    │
│                                              │
│  Replace placeholders with original values   │
│  from local SQLite mapping                   │
└──────────────────────┬──────────────────────┘
                       ↓
          Restored response → User
```

### 3.2 Anonymization Categories

| Category | Pattern | Example | Placeholder |
|---|---|---|---|
| Credentials | API keys, tokens, passwords from agent config | `sk-ant-api03...` | `__VAULT_CREDENTIAL_1__` |
| Email | RFC 5322 email addresses | `user@example.de` | `__VAULT_EMAIL_1__` |
| Phone | German phone numbers (+49, 0049, 0...) | `+49 171 1234567` | `__VAULT_PHONE_1__` |
| Names | German name patterns (Herr/Frau Vorname Nachname) | `Herr Max Mustermann` | `__VAULT_NAME_1__` |

### 3.3 Anonymization Levels

Configurable per agent in the Dashboard wizard:

| Level | What is anonymized |
|---|---|
| **strict** (default) | All: credentials + emails + phones + names |
| **medium** | Credentials + emails + phones (names pass through) |
| **minimal** | Credentials only (API keys, passwords, tokens) |

### 3.4 Mapping Storage

The placeholder-to-original mapping is stored exclusively in the agent's local SQLite
database (`storage/agents/{name}/memory.db`, table `vault_mappings`).

- Mappings **never leave the server**
- Each anonymization session has a unique session ID
- Mappings are keyed by session, enabling full traceability

---

## 4. Audit Logging — Proof of Compliance

### 4.1 External API Audit Log

Every external API interaction is logged to:
```
storage/agents/{agent_name}/external_api_audit.log
```

Each entry contains:
- **Timestamp** (UTC)
- **Agent name**
- **Session ID** (links to vault_mappings for full traceability)
- **Event type**: SYSTEM_PROMPT, REQUEST, RESPONSE, or ERROR
- **Model** used (e.g. `claude-sonnet-4-20250514`)
- **Anonymization count** (how many PII items were replaced)
- **Complete payload** — the FULL anonymized text, never truncated (CR-139)

The audit log is the **legally binding proof** of exactly what data left and entered the server.
Every external API interaction is logged with: the system prompt sent, the complete anonymized
user prompt, and the complete response received. Multiline payloads are wrapped in
`PAYLOAD`/`END_PAYLOAD` markers for automated parsing.

### 4.2 Sample Audit Entry

```
[2026-03-22 14:27:49] [kai] session=086f8f0c5271 SYSTEM_PROMPT | model=anthropic/claude-sonnet-4-20250514
  PAYLOAD:
  | You are an expert assistant supporting an AIMOS agent (an on-premise AI system). [...]
  END_PAYLOAD
[2026-03-22 14:27:49] [kai] session=086f8f0c5271 REQUEST | model=anthropic/claude-sonnet-4-20250514 anon_items=2 prompt_len=144
  PAYLOAD:
  | Kontext: channel=text, zeit=2026-03-20 14:26:40
  |
  | Frage: Was sind Best Practices für VRAM-Management bei lokalen LLM-Deployments?
  END_PAYLOAD
[2026-03-22 14:27:58] [kai] session=086f8f0c5271 RESPONSE | model=anthropic/claude-sonnet-4-20250514 response_len=1158
  PAYLOAD:
  | ## Wichtige VRAM-Management Strategien:
  | [complete response text...]
  END_PAYLOAD
```

This proves:
- Exactly what system instructions the external LLM received
- The complete user prompt with all PII replaced by placeholders
- The complete response — no data was received that isn't documented
- The external API (Anthropic) only received anonymized data
- The original PII values remained on the local server (in vault_mappings)

### 4.3 General Agent Audit Log

All tool executions (including local tools) are logged to:
```
storage/agents/{agent_name}/api_audit.log
```

This provides a complete trail of agent actions: LLM calls, tool invocations, results.

---

## 5. Data Residency Summary

| Data Type | Storage Location | Leaves Server? |
|---|---|---|
| Chat histories | PostgreSQL (local) | No |
| Agent configuration | PostgreSQL (local) | No |
| Long-term memory | SQLite per agent (local) | No |
| PII vault mappings | SQLite per agent (local) | No |
| Audit logs | Filesystem (local) | No |
| Anonymized prompts | Transmitted to external API | Yes — **without PII** |
| API responses | Received, deanonymized locally | Response stored locally only |
| Telegram messages | Transit via Telegram servers | Yes (standard Telegram transport) |
| Emails | Transit via configured SMTP/IMAP | Yes (standard email transport) |

---

## 6. Technical Safeguards

| Safeguard | Implementation |
|---|---|
| No automatic external calls | `ask_external` is an explicit tool — agent must decide to use it |
| Mandatory anonymization | PII Vault runs before every external API call — cannot be bypassed |
| Credential protection | All env_secrets (API keys, passwords) are scanned and replaced |
| Audit trail | Every external call logged with anonymized payload as proof |
| Local-first processing | Qwen 3.5:27b runs entirely on local GPU — no data leaves for standard queries |
| Per-agent isolation | Each agent has its own memory.db, workspace, and audit log |
| Configurable anonymization | Operators can set strict/medium/minimal per agent |

---

## 7. Compliance Statement

AIMOS is designed to comply with GDPR (EU 2016/679) Article 25 (Data Protection by Design)
and Article 32 (Security of Processing):

- **Minimization**: External API calls are minimized — only on explicit escalation
- **Pseudonymization**: PII is replaced with non-reversible placeholders before external transmission
- **Transparency**: Full audit trail of all external API interactions
- **Data Residency**: All personal data remains on the local server
- **Access Control**: Per-agent isolation prevents cross-agent data access

For data processing agreements with external API providers (Anthropic, OpenRouter, OpenAI),
refer to their respective DPA documentation. AIMOS ensures that no personal data is included
in the transmitted prompts.

---

## 8. Files Reference

| File | Purpose |
|---|---|
| `core/skills/skill_hybrid_reasoning.py` | External LLM gateway + PII Vault implementation |
| `core/prompts/core_system.txt` | Agent behavioral rules (escalation criteria) |
| `storage/agents/{name}/external_api_audit.log` | Per-agent external API audit trail |
| `storage/agents/{name}/memory.db` → `vault_mappings` | PII placeholder ↔ original mapping |
| `storage/agents/{name}/api_audit.log` | General tool execution audit trail |
| `docs/MEMORY_ARCHITECTURE.md` | Agent memory system documentation |
| `docs/ARCHITECTURE.md` | Full system architecture |
