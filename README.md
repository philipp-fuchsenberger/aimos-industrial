# AIMOS — AI Managed Operating System

**Multi-agent AI platform for industrial intelligence.** Runs entirely on your own hardware. No cloud dependency, no license fees, no data leaving your server.

## What is AIMOS?

AIMOS is an operating system for AI assistants ("agents") that automate business processes. Each assistant has its own memory, skills, and personality — managed by a central orchestrator on a single GPU.

**Key Features:**
- Autonomous AI assistants with persistent long-term memory
- Hybrid memory search (FTS5 + vector embeddings + RRF fusion)
- Memory dreaming (idle-time consolidation and fact extraction)
- Native LLM tool-calling via Ollama (Qwen, Llama, Mistral, etc.)
- Execution Rings (3 trust levels per assistant)
- PII Vault (automatic anonymization for external API calls)
- Human-in-the-Loop (assistants prepare, humans decide)
- Communication: Telegram, Email, Voice (Whisper STT + Piper TTS)
- 15 pluggable skills (web search, email, file ops, ERP, CRM, scheduling, web automation)
- Agent portability (OAP export/import, MCP Bridge, A2A Cards)
- EU AI Act, GDPR, ISO 9001/27001, GoBD ready

## Architecture

```
start_clean.sh
  |
  +-- Dashboard (FastAPI :8080)       # Web UI, agent wizard, monitoring
  +-- Shared Listener                 # Telegram + IMAP polling -> DB
  +-- Orchestrator (daemon)           # 2s scan loop, GPU management
        |
        +-- Spawns: main.py --id {agent} --mode auto
              |
              +-- Agent Kernel
              |     +-- LLM (Ollama API, native tool-calling)
              |     +-- Skills (brave_search, email, file_ops, ...)
              |     +-- Memory (SQLite: hybrid FTS5 + vector search)
              |     +-- Output Firewall (CJK, thought-leak filter)
              |
              +-- Connectors
                    +-- Telegram (via DB relay)
                    +-- Email (IMAP/SMTP)
                    +-- Voice (Whisper + Piper)
```

## Quick Start

### Prerequisites
- Ubuntu 24.04 LTS
- NVIDIA GPU (24 GB+ VRAM) with driver 550+
- PostgreSQL 15+
- Ollama

### Installation

```bash
# Clone
git clone https://github.com/philipp-fuchsenberger/aimos-industrial.git
cd aimos-industrial

# Python environment
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Pull an LLM model
ollama pull qwen2.5:14b

# Configure
cp .env.example .env
# Edit .env with your PostgreSQL credentials and dashboard password

# Start
./start_clean.sh
```

Dashboard: `http://localhost:8080`

### Create Your First Agent

1. Open the Dashboard
2. Click **"New Agent"**
3. Fill in: name, system prompt, skills, Telegram bot token
4. Save — the Orchestrator manages the lifecycle automatically

See [docs/DEPLOYMENT_RUNBOOK.md](docs/DEPLOYMENT_RUNBOOK.md) for the full installation guide.

## Hardware Requirements

| Component | Minimum | Recommended |
|---|---|---|
| GPU | RTX 3090 (24 GB) | RTX 4090 / RTX 5090 |
| RAM | 32 GB | 64 GB |
| Storage | 256 GB NVMe | 512 GB NVMe |
| CPU | 8 cores | 16 cores |

## Documentation

| Document | Description |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | System architecture, DB schema, skills |
| [MEMORY_ARCHITECTURE.md](docs/MEMORY_ARCHITECTURE.md) | Tiered memory, dreaming, hybrid search |
| [DEPLOYMENT_RUNBOOK.md](docs/DEPLOYMENT_RUNBOOK.md) | Step-by-step installation guide |
| [GDPR_COMPLIANCE.md](docs/GDPR_COMPLIANCE.md) | Data protection, PII vault, audit trails |
| [LLM_REQUEST_ANATOMY.md](docs/LLM_REQUEST_ANATOMY.md) | How an LLM call is constructed |

## License

MIT License — see [LICENSE](LICENSE)

## About

Developed by [EcoHub Muenchen GmbH](https://aimos-industrial.com)

