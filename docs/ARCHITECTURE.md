# AIMOS Architecture — v4.6.0 (Dashboard Redesign + Internal Msg Fix + Website Overhaul)

**Artificial Intelligence Managed Operating System**
**Author:** Philipp Fuchsenberger | **Hardware:** Alfred-Server, RTX 4090 24GB

---

## System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    External Users                           │
│              Telegram / Email / Voice                       │
└──────────────────────┬──────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────┐
│  scripts/shared_listener.py         (Zero VRAM, I/O only)   │
│  - Telegram long-polling (all bots simultaneously)          │
│  - IMAP mailbox polling (60s interval)                      │
│  - Outbound sender (agent replies from DB → Telegram API)   │
│  - Writes to: pending_messages                              │
└──────────────────────┬──────────────────────────────────────┘
                       │ INSERT pending_messages
┌──────────────────────▼──────────────────────────────────────┐
│  core/orchestrator.py               (Daemon, 2s poll cycle) │
│  - Scans pending_messages for work                          │
│  - Handles requested_state from Dashboard                   │
│  - Spawns agents via subprocess.Popen (ONLY process spawner)│
│  - Zombie detection, PID verification, rate limiting        │
│  - Self-healing: kills duplicates, verifies PIDs on startup │
└──────────────────────┬──────────────────────────────────────┘
                       │ subprocess: main.py --id X --mode orchestrator
┌──────────────────────▼──────────────────────────────────────┐
│  main.py → core/agent_base.py       (Agent Process)         │
│  - Loads config + secrets from DB at boot                   │
│  - Loads only skills listed in config.skills              │
│  - think(): LLM call → native tool-calling → response        │
│  - dispatch_response(): routes reply by message kind        │
│  - All modes: replies → DB (outbound_telegram via relay)    │
│  - Watchdog: 600s idle → auto-shutdown                      │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  core/dashboard/app.py              (FastAPI, port 8080)     │
│  - Infrastructure monitoring only (GPU, CPU, agent status)  │
│  - Sets requested_state in DB (NO subprocess.Popen)         │
│  - Agent Wizard (create/edit config + secrets)              │
│  - Orchestrator toggle (auto/manual mode)                   │
└─────────────────────────────────────────────────────────────┘
```

## Competency Split (Strict)

| Component | May | May NOT |
|---|---|---|
| **Orchestrator** | Spawn/kill agents, read all tables, verify PIDs | Modify agent config |
| **Dashboard** | Set requested_state, edit config when agent offline | Spawn/kill processes |
| **Agent** | Read own messages, write outbound replies, send_to_agent (inter-agent) | Access other agents' data directly |
| **Shared Listener** | Write to pending_messages, send outbound | Start/stop agents |

## File Structure

```
AIMOS/
├── main.py                     # Agent bootloader (PID singleton)
├── start_clean.sh              # Kill all → enable orchestrator → start system
├── .env                        # Secrets fallback (gitignored)
├── agents/                     # Agent profiles (documentation, DB is truth)
│   ├── neo.py                  # Telegram + Web-Search (Philipp's assistant)
│   ├── merve.py                # Telegram + Email + Office
│   ├── kai.py                  # General purpose
│   ├── kral.py                 # Turkish business assistant (Ugur's agent)
│   └── leyla.py                # Voice assistant (Jabra hardware)
├── core/
│   ├── config.py               # Config + SecretFilter + RotatingFileHandler
│   ├── agent_base.py           # Agent kernel: DB, LLM, tools, dispatch
│   ├── orchestrator.py         # Daemon process manager (the boss)
│   ├── prompts/
│   │   └── core_system.txt     # Immutable agent behavioral rules (English)
│   ├── connectors/
│   │   ├── base.py             # AIMOSConnector ABC
│   │   └── telegram.py         # Send: message, photo, voice, typing, document
│   ├── skills/
│   │   ├── base.py             # BaseSkill ABC (workspace, config_fields, memory_db)
│   │   ├── brave_search.py     # web_search
│   │   ├── email_io.py         # read_emails, send_email (IMAP/SMTP TLS 1.2+)
│   │   ├── file_ops.py         # list_workspace, convert_document, extract_pdf
│   │   ├── voice_io.py         # STT (Faster-Whisper) + TTS (Piper)
│   │   ├── skill_shared_storage.py  # list/read/write on local/SMB mounts
│   │   ├── skill_scheduler.py       # set_reminder, list_jobs (Cronjobs)
│   │   ├── skill_hybrid_reasoning.py # External LLM gateway + PII Vault
│   │   ├── skill_mail_monitor.py    # POP3 read-only fetch + local archive
│   │   ├── skill_web_automation.py  # Playwright headless browser + login flows
│   │   ├── skill_remote_storage.py  # SFTP over Tailscale (CR-097)
│   │   ├── skill_persistence.py     # Open request tracking + auto-remind (CR-104)
│   │   ├── skill_football_observer.py # Galatasaray RSS tracker (CR-102)
│   │   ├── skill_tr_calendar.py     # Turkish holidays/Ramadan (CR-103)
│   │   ├── skill_eta_accounting.py  # ETA accounting base class (CR-132)
│   │   ├── skill_eta_firebird.py    # Firebird variant (port 3050)
│   │   ├── skill_eta_mssql.py       # MSSQL variant (port 1433)
│   │   ├── eta_mapping.json         # SQL queries (adjustable per installation)
│   │   └── mcp_bridge.py           # MCP manifest + A2A Agent Cards (CR-121)
│   └── dashboard/
│       ├── app.py              # FastAPI + DB helpers + GPU cache thread
│       ├── routes.py           # All HTTP endpoints
│       └── templates/          # Jinja2 HTML (dashboard.html, wizard.html)
├── scripts/
│   ├── shared_listener.py      # Telegram + IMAP + outbound relay + agent watcher
│   ├── voice_listener.py       # Always-on VAD → Whisper → DB for voice agents
│   ├── validate_cr.py          # End-to-end CR validation (inject → process → verify)
│   ├── safe_restart.sh         # Safe code deployment (waits for idle, preserves messages)
│   ├── agent_export.py         # OAP Export/Import CLI (CR-121)
│   ├── test_monitor.py         # Unified real-time log monitor
│   └── kill_all.sh             # Nuclear SIGKILL + VRAM flush
├── storage/
│   └── agents/                 # Per-agent workspaces (gitignored)
│       ├── neo/                # Neo's workspace
│       │   ├── public/         # Cross-agent readable folder
│       │   └── memory.db       # Per-agent SQLite (memories, skill_state, vault_mappings)
│       └── merve/              # Merve's workspace
│           ├── public/         # Cross-agent readable folder
│           └── memory.db       # Per-agent SQLite
├── tools/
│   └── validate_requirements.py
├── docs/                       # All documentation
├── Marketing/                  # Website source (aimos-industrial.com)
│   ├── index.html              # German landing page
│   ├── agenten.html            # Agent architecture (7 chapters)
│   ├── technik.html            # Technical details (7 chapters)
│   ├── compliance.html         # Compliance & GDPR (4 chapters)
│   ├── preise.html             # Pricing + ROI analysis
│   ├── kontakt.html            # Contact form (Formspree)
│   ├── impressum.html          # Legal notice
│   ├── datenschutz.html        # Privacy policy
│   ├── en/                     # English
│   ├── tr/                     # Turkish
│   ├── fr/                     # French
│   ├── es/                     # Spanish
│   └── it/                     # Italian
└── logs/                       # Runtime logs (gitignored, rotating 10MB×5)
```

## Website Deployment

- **Domain**: aimos-industrial.com (Namecheap DNS → GitHub Pages)
- **Repo**: `philipp-fuchsenberger/aimos-industrial.com` (GitHub Pages, separate from code repo)
- **Source**: `Marketing/` folder in this (private) AIMOS repo
- **Deploy**: Copy `Marketing/*` → public repo root → `git push`
- **Code Repo**: `philipp-fuchsenberger/aimos-industrial` (sanitized open-source version)

## Database Schema

### agents
| Column | Type | Purpose |
|---|---|---|
| name | VARCHAR | Unique identifier (lowercase) |
| status | VARCHAR | offline / starting / active / idle |
| config | JSONB | skills, character, system_prompt, execution_strategy |
| env_secrets | JSONB | TELEGRAM_BOT_TOKEN, EMAIL_*, LLM_TEMPERATURE |
| pid | INTEGER | OS process ID (NULL when offline) |
| requested_state | TEXT | Dashboard command: 'active' or 'offline' (NULL = no request) |
| updated_at | TIMESTAMPTZ | Heartbeat (updated every poll cycle ~2s) |

### pending_messages
| Column | Type | Purpose |
|---|---|---|
| agent_name | TEXT | Target agent |
| sender_id | BIGINT | Telegram chat_id (0 = system/email) |
| thread_id | TEXT | Isolation key (e.g. "tg_12345" or "email_user@example.com") |
| content | TEXT | Message payload |
| kind | TEXT | telegram / telegram_voice / telegram_doc / email / dashboard / internal / scheduled_job / outbound_telegram / outbound_telegram_doc |
| processed | BOOLEAN | FALSE = pending, TRUE = claimed/sent |

### agent_jobs (CR-063)
| Column | Type | Purpose |
|---|---|---|
| agent_name | TEXT | Target agent |
| scheduled_time | TIMESTAMPTZ | When to fire |
| task_prompt | TEXT | Prompt injected as pending_message |
| status | TEXT | pending / fired |
| source | TEXT | 'agent' (self-set) — anti-recursion flag |

### global_settings
| Key | Value | Purpose |
|---|---|---|
| orchestrator_mode | {"enabled": true/false} | Auto-pilot toggle |
| secret.BRAVE_API_KEY | "BSA..." | Shared across all agents |

## Skills

| Skill | Tools | System Dependencies |
|---|---|---|
| brave_search | web_search(query) | aiohttp |
| email_io | read_emails(folder,limit), send_email(to,subject,body) | (stdlib) |
| file_ops | list_workspace, convert_document, extract_pdf_text | libreoffice, pandoc, poppler, tesseract |
| voice_io | listen() → STT, speak() → TTS | faster-whisper (CPU), piper-tts, sounddevice |
| shared_storage | list_shared(path), read_shared(path,filename), write_shared(path,filename,content) | (stdlib) |
| scheduler | set_reminder(when,task), list_jobs() | asyncpg |
| hybrid_reasoning | ask_external(question,context) — PII-anonymized gateway to Anthropic/OpenRouter | httpx |
| mail_monitor | fetch_user_mail(), search_mail(query), read_mail(mail_id) — POP3 read-only | (stdlib) |
| web_automation | web_login_and_extract(flow_name), web_browse(url,selector) — Playwright headless | playwright |
| remote_storage | remote_list_files, remote_read_file, remote_write_file, remote_setup_guide — SFTP over Tailscale | paramiko |
| persistence | track_request, check_open_requests, close_request — open request tracking with auto-remind | asyncpg |
| football_observer | check_gs_results — Galatasaray RSS tracker with mood system | aiohttp |
| tr_calendar | check_today — Turkish holidays, Ramadan, configurable family days | (stdlib) |
| eta_firebird | get_customer_balance, list_unpaid_invoices, search_transactions, get_daily_summary — ETA V8 | fdb |
| eta_mssql | get_customer_balance, list_unpaid_invoices, search_transactions, get_daily_summary — ETA-SQL | pymssql |

### System Tools (registered for all agents, no skill dependency)

| Tool | Purpose |
|---|---|
| system_status | Ollama + VRAM status |
| current_time | UTC + local time + weekday |
| write_file(filename, content) | Create text files in agent workspace |
| read_file(filename) | Read text/docx/pdf/xlsx files from workspace |
| send_telegram_file(chat_id, filename) | Send workspace file as Telegram document |
| send_telegram_message(chat_id, text) | Proactive Telegram messaging |
| send_voice_message(chat_id, text, voice) | Piper TTS → OGG → Telegram (5 voices: de_female/male, en_female/male, tr_male) |
| send_to_agent(agent_name, message) | Inter-agent communication (CR-065) |
| remember(key, value, category, importance) | Store fact in long-term memory |
| recall(query) | Search long-term memory |
| forget(key) | Delete memory entry |
| check_credentials | Show set/missing status for configured secrets |
| update_credential(key, value) | Update whitelisted credentials (if enabled) |

## Database Architecture

```
PostgreSQL (aimos-db)          SQLite (per-agent)
┌─────────────────────┐       ┌──────────────────────────────┐
│ agents              │       │ storage/agents/{name}/memory.db │
│ pending_messages    │       │ ├── memories (tiered, scored)   │
│ aimos_chat_histories│       │ ├── skill_state (per-skill)    │
│ global_settings     │       │ ├── agent_log (private logs)   │
│ agent_jobs          │       │ └── vault_mappings (PII DLP)   │
└─────────────────────┘       └──────────────────────────────┘
  Shared relay + config         Private agent-local state
```

## VRAM Strategy

| Agent Type | Model | VRAM Behavior | Watchdog |
|---|---|---|---|
| Support | qwen2.5:14b (~9 GB) | sequential, keep_alive=30m, ~30K context, cb=0 (Deep Memory, 512 predict) | 10min idle → shutdown |
| Innendienst | qwen2.5:32b (~19 GB) | sequential, keep_alive=30m, ~10K context, cb=0 (Deep Memory, 512 predict) | 10min idle → shutdown |
| Voice (Leyla) | qwen2.5:14b + Whisper on CPU | 10K context, cb=2 (Balanced, 1536 predict) | Disabled — stays alive |

Sequential operation: only 1 agent at a time. Model stays loaded via keep_alive=30m between agent switches.
Dual-model architecture: Support uses the lighter 14B model for fast responses with large context; Innendienst uses the 32B model for complex business tasks.
Whisper STT runs on CPU (medium model, ~5s) — no VRAM conflict.

## DB Timeout Protection (CR-138)

Root cause of recurring agent hangs: asyncpg pool had no `command_timeout`. Any DB stall caused indefinite waits.

| Protection | Timeout | Effect |
|---|---|---|
| `create_pool(command_timeout=15)` | 15s | Every DB query auto-aborts |
| `pool.acquire(timeout=5)` | 5s | Pool exhaustion → skip cycle |
| Startup sequence | 60s | Agent aborts if DB unreachable |
| `_persist_message()` | 10s | Chat history write → skip on timeout |
| CR-120 zombie detection | — | Safety net: kills stuck processes |
| CR-137 process watchdog | 5min | Safety net: kills 0% CPU + stale heartbeat |

## Execution Rings — Agent Trust Levels (CR-142)

| Ring | Name | Tools | Use Case |
|------|------|-------|----------|
| 0 | Read Only | search, recall, status, file read, accounting queries | Monitoring agents, read-only dashboards |
| 1 | Standard | send messages, write files, set reminders, email | Business assistants, communication agents |
| 2 | Full Access | external APIs, credential changes, web automation | Trusted personal agents, admin agents |

Each agent has `config.max_ring` (default: 2). Policy check runs before every tool execution.
Blocked tools return an error message — the agent can adapt (e.g., ask a higher-ring agent for help).

## Agent Memory (CR-081)

See `docs/MEMORY_ARCHITECTURE.md` for full design.

Tiered memory with relevance scoring (importance * recency * frequency).
Top memories auto-injected into system prompt at every think() call (count depends on cognitive_balance: 8-50).
Tools: `remember(key, value, category, importance)`, `recall(query)`, `forget(key)`.

**Hybrid Search (CR-140)**: `recall()` combines FTS5 keyword search + vector cosine similarity
(all-MiniLM-L6-v2, 384 dims, CPU) via Reciprocal Rank Fusion (RRF). Finds semantically related
memories even without keyword overlap. Graceful fallback to keyword-only if embeddings unavailable.

**Dreaming (Memory Consolidation + Extraction)**: When an agent is idle >1h, the Orchestrator
triggers a dream cycle. Phase 0 uses a **local LLM call** to extract facts from recent conversations
(corrections, preferences, decisions, names). Phases 1-4 consolidate, resolve contradictions,
decay stale memories, and clean hallucinations (pure SQLite, no LLM). See `docs/MEMORY_ARCHITECTURE.md`.

## GDPR Compliance

See `docs/GDPR_COMPLIANCE.md` for full documentation.

All external API calls are anonymized (PII Vault) and audit-logged
(`storage/agents/{name}/external_api_audit.log`). No personal data leaves the server.

## PII Vault (CR-075)

```
Agent prompt → Vault.anonymize() → External API (OpenRouter/OpenAI)
                  ↓                        ↓
         memory.db vault_mappings    API response
                  ↓                        ↓
              Vault.deanonymize() ← raw response
                  ↓
         Restored text → User
```

Levels: strict (all PII), medium (credentials+emails+phones), minimal (credentials only).

## Dynamic Wizard

Skills declare their config fields via `BaseSkill.config_fields()`.
The Dashboard wizard reads `SKILL_REGISTRY` at render time and generates
config panels dynamically — no hardcoded UI changes needed for new skills.

## Message Flow (Orchestrator Mode)

```
User → Telegram → shared_listener → pending_messages (kind=telegram)
  → orchestrator detects → spawns agent
  → agent: poll_pending → think() → dispatch_response
  → outbound_telegram → DB → shared_listener → Telegram → User
```

## Thread Architecture (CR-207)

AIMOS uses thread-based isolation to support multiple concurrent conversations per agent.

- **thread_id** column in `pending_messages` and `aimos_chat_histories` links messages to a specific conversation thread
- Format: `tg_{chat_id}` for Telegram, `email_{address}` for email
- Each thread maintains its own chat history — no cross-contamination between customers
- The `/k` Telegram command lets helpdesk operators switch between customer threads without losing context
- Support agent handles image analysis directly via Claude Vision (hybrid_reasoning skill) — no separate backoffice agent needed

## Credentials

| Location | Contains | Scope |
|---|---|---|
| agents.env_secrets (DB) | TELEGRAM_BOT_TOKEN, EMAIL_* | Per agent |
| global_settings (DB) | secret.BRAVE_API_KEY | All agents |
| .env file (fallback) | All keys | Host process |

### Dashboard Environment Variables

| Variable | Default | Description |
|---|---|---|
| `AIMOS_DASHBOARD_PASSWORD` | `aimos2026` | HTTP Basic Auth password for dashboard |
| `AIMOS_CORS_ORIGIN` | `*` | Allowed CORS origin (CR-162/CR-175). Set to specific origin in production, e.g. `https://aimos.local` |
