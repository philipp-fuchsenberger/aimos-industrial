# AIMOS Deployment Runbook

**Document:** DEPLOY-AIMOS-2026-001
**Target:** Fresh SovereignNode installation
**Prepared:** 2026-03-25
**Author:** Dr. Philipp Fuchsenberger / EcoHub Muenchen GmbH
**Prerequisites:** Ubuntu 24.04 LTS, NVIDIA GPU (24 GB+ VRAM), Internet connection

---

## 1. Hardware Requirements

| Component | Minimum | Recommended |
|---|---|---|
| GPU | NVIDIA RTX 3090 (24 GB VRAM) | NVIDIA RTX 4090/5090 (24-32 GB VRAM) |
| RAM | 32 GB DDR4 | 64 GB DDR5 |
| Storage | 256 GB NVMe SSD | 512 GB NVMe SSD |
| CPU | 8 cores (AMD Ryzen 7 / Intel i7) | 16 cores (AMD Ryzen 9 / Intel i9) |
| Network | Gigabit Ethernet | Gigabit Ethernet, static IP or DHCP reservation |

**VRAM Budget (Qwen 3.5:27b Q4_K_M):**

| Component | VRAM Usage |
|---|---|
| LLM Model (loaded) | ~17 GB |
| Whisper STT (medium) | ~2 GB (loaded on demand, LLM unloaded first) |
| CUDA overhead | ~1 GB |
| **Total peak** | **~20 GB** |

> Only one agent holds the GPU at a time (GPU mutex via `/tmp/aimos_gpu.lock`). Multi-agent concurrency is time-sliced, not parallel.

---

## 2. OS Preparation

### 2.1 Base System

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install core dependencies
sudo apt install -y \
    python3.12 python3.12-venv python3.12-dev \
    postgresql postgresql-contrib \
    git curl wget \
    ffmpeg \
    build-essential \
    libpq-dev

# Optional: LibreOffice + Pandoc for document conversion (file_ops skill)
sudo apt install -y libreoffice-calc libreoffice-writer pandoc

# Optional: Playwright dependencies for web automation skill
sudo apt install -y libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libgbm1
```

### 2.2 Firewall

```bash
# Allow SSH, Dashboard, and Ollama (local only)
sudo ufw allow 22/tcp
sudo ufw allow 8080/tcp
sudo ufw enable
```

> Port 8080 is the AIMOS Dashboard. Restrict access to your network if exposed to the internet. Ollama (port 11434) should remain localhost-only.

---

## 3. NVIDIA Driver + CUDA

### 3.1 Install Driver

```bash
# Install NVIDIA driver (550 series recommended for RTX 30xx/40xx/50xx)
sudo apt install -y nvidia-driver-550

# Reboot required
sudo reboot
```

### 3.2 Verify Installation

```bash
nvidia-smi
```

Expected output should show:
- Driver version >= 550.x
- GPU name (e.g., NVIDIA GeForce RTX 3090)
- 24576 MiB total memory

> CUDA toolkit installation is NOT required. Ollama and Python packages (nvidia-cublas, nvidia-cudnn) bundle their own CUDA libraries. The `start_clean.sh` script sets `LD_LIBRARY_PATH` automatically.

---

## 4. Ollama Installation

### 4.1 Install Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

### 4.2 Pull the LLM Model

```bash
# Primary model — Qwen 3.5:27b (best tool-following on 24 GB VRAM)
ollama pull qwen3.5:27b
```

### 4.3 Verify Ollama

```bash
# Check Ollama is running
curl -s http://127.0.0.1:11434/api/tags | python3 -m json.tool

# Test inference
ollama run qwen3.5:27b "Hello, respond in one sentence."
```

### 4.4 Ollama Configuration (Optional)

```bash
# If Ollama needs to listen on a custom address (default: 127.0.0.1:11434)
sudo systemctl edit ollama
# Add:
# [Service]
# Environment="OLLAMA_HOST=0.0.0.0:11434"
sudo systemctl restart ollama
```

---

## 5. PostgreSQL Setup

### 5.1 Create Database and User

```bash
# Create user and database
sudo -u postgres createuser aimos_user -P
# Enter password when prompted — save this for .env configuration

sudo -u postgres createdb aimos -O aimos_user
```

### 5.2 Grant Permissions

```bash
sudo -u postgres psql -d aimos -c "GRANT ALL PRIVILEGES ON DATABASE aimos TO aimos_user;"
sudo -u postgres psql -d aimos -c "GRANT ALL ON SCHEMA public TO aimos_user;"
```

### 5.3 Verify Connection

```bash
psql -h 127.0.0.1 -U aimos_user -d aimos -c "SELECT 1;"
```

> AIMOS uses self-healing schema management (`_ensure_schema()` in agent_base.py). All required tables (`agents`, `pending_messages`, `aimos_chat_histories`, `global_settings`, `agent_jobs`) are created automatically on first agent boot. No manual schema migration needed.

---

## 6. AIMOS Installation

### 6.1 Clone Repository

```bash
cd /opt  # or your preferred installation directory
git clone https://github.com/philipp-fuchsenberger/aimos-industrial.git aimos
cd aimos
```

### 6.2 Python Virtual Environment

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 6.3 Verify Installation

```bash
# Check core imports
python3 -c "from core.config import Config; print(Config())"
python3 -c "from core.skills import SKILL_REGISTRY; print(list(SKILL_REGISTRY.keys()))"
```

---

## 7. Configuration

### 7.1 Environment File

Create `.env` in the AIMOS root directory:

```bash
# /opt/aimos/.env

# ── Database ──────────────────────────────────────────────────
PG_HOST=127.0.0.1
PG_PORT=5432
PG_DB=aimos
PG_USER=aimos_user
PG_PASSWORD=your_secure_password_here

# ── LLM (Ollama) ─────────────────────────────────────────────
LLM_BASE_URL=http://127.0.0.1:11434
LLM_MODEL=qwen3.5:27b
LLM_KEEP_ALIVE=30m
LLM_TEMPERATURE=0.7
LLM_NUM_CTX=14336

# ── Agent Defaults ────────────────────────────────────────────
MAX_TOOL_ROUNDS=5
HISTORY_LIMIT=50
POLL_INTERVAL=2.0

# ── Dashboard ─────────────────────────────────────────────────
AIMOS_DASHBOARD_PASSWORD=your_dashboard_password_here

# ── Output Firewall ───────────────────────────────────────────
CLEAN_CJK=true

# ── Whisper STT (optional, for voice agents) ─────────────────
WHISPER_MODEL=medium
```

### 7.2 Configuration Hierarchy

AIMOS resolves configuration values in this order (highest priority first):

1. **Agent-level config** — JSON in `agents.config` column (set via wizard)
2. **global_settings table** — Key-value pairs in PostgreSQL
3. **`.env` file** — Root `.env` overrides `core/.env`
4. **Hardcoded defaults** — In `core/config.py`

> Secrets (API keys, tokens) are NEVER hardcoded. Use agent config or global_settings for per-agent secrets. The `check_credentials` system tool lets agents verify which keys are configured.

---

## 8. First Agent Creation

### 8.1 Start the System

```bash
cd /opt/aimos
./start_clean.sh
```

### 8.2 Access the Dashboard

Open `http://<server-ip>:8080` in your browser. Log in with:
- Username: `admin`
- Password: value of `AIMOS_DASHBOARD_PASSWORD` from `.env`

### 8.3 Create an Agent via Wizard

1. Click **"New Agent"** in the dashboard
2. Fill in the wizard:
   - **Name:** e.g., `neo` (lowercase, no spaces)
   - **Type:** Work Agent (max context, deep memory) or Voice Assistant (parallel, Jabra)
   - **System Prompt:** Define the agent's personality and role
   - **Skills:** Select from available skills (brave_search, email, file_ops, scheduler, etc.)
   - **Telegram Token:** Paste BotFather token (create via [@BotFather](https://t.me/BotFather))
   - **Allowed Chat IDs:** Your Telegram user ID(s)
3. Click **Save** — agent is registered in DB and workspace directories are created

### 8.4 Test the Agent

Send a message to your bot on Telegram. Within one orchestrator cycle (2s), the agent should spawn, process the message, and reply.

---

## 9. Startup and Shutdown

### 9.1 Normal Start

```bash
cd /opt/aimos
./start_clean.sh
```

This script performs:
1. Kills all existing AIMOS processes (dashboard, orchestrator, listener, agents)
2. Removes stale PID files from `/tmp/aimos_agent_*.pid`
3. Activates Python venv and sets CUDA `LD_LIBRARY_PATH`
4. Truncates log files
5. Enables orchestrator in DB, expires stale messages (> 5 min), resets all agents to `offline`
6. Starts three daemon processes:
   - **Dashboard** (port 8080) — `python3 -m core.dashboard.app`
   - **Shared Listener** — `python3 scripts/shared_listener.py` (Telegram + IMAP relay)
   - **Orchestrator** — `python3 -m core.orchestrator` (agent lifecycle manager)

### 9.2 Start with Voice (Optional)

```bash
# Start with hardware voice listener (Jabra speaker)
./start_clean.sh --voice

# Or via environment variable
AIMOS_VOICE=1 VOICE_AGENT=leyla ./start_clean.sh
```

### 9.3 Shutdown

```bash
# Graceful: disable orchestrator, agents will self-terminate via watchdog
# Via dashboard: toggle orchestrator OFF

# Immediate: kill all processes
pkill -f "core.dashboard"
pkill -f "core.orchestrator"
pkill -f "shared_listener"
pkill -f "main.py.*--id"
rm -f /tmp/aimos_agent_*.pid
```

---

## 10. Smoke Test Checklist

Run these checks after every deployment or restart:

- [ ] Dashboard accessible at `http://<server-ip>:8080`
- [ ] HTTP Basic Auth working (rejects wrong credentials)
- [ ] GPU metrics visible in dashboard (VRAM bar, temperature)
- [ ] Orchestrator shows as "enabled" in dashboard
- [ ] At least one agent registered in agent list
- [ ] Agent starts within 5s after sending Telegram message
- [ ] Agent responds within 30s (first response may take 10s for model load)
- [ ] Memory persists: tell agent to remember something, restart, ask for recall
- [ ] Validation script passes: `python scripts/validate_cr.py --agent neo`
- [ ] Log files populating: `tail -f logs/orchestrator.log`

---

## 11. Maintenance

### 11.1 Daily

```bash
# Check system health
python scripts/validate_cr.py --agent neo --dry-run

# Review logs for errors
grep -i "error\|fail\|exception" logs/orchestrator.log | tail -20
grep -i "error\|fail\|exception" logs/shared_listener.log | tail -20
```

### 11.2 Weekly

```bash
# PostgreSQL maintenance
sudo -u postgres vacuumdb --analyze aimos

# Backup database
pg_dump -h 127.0.0.1 -U aimos_user aimos | gzip > /backup/aimos_$(date +%Y%m%d).sql.gz

# Check disk usage (model weights + agent workspaces)
du -sh /opt/aimos/storage/
du -sh ~/.ollama/models/
```

### 11.3 Monthly

```bash
# Update Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Update Python dependencies
cd /opt/aimos
source venv/bin/activate
pip install --upgrade -r requirements.txt

# After updates, always restart cleanly
./start_clean.sh
```

### 11.4 Log Management

Logs use `RotatingFileHandler` (10 MB per file, 5 backups). Location: `/opt/aimos/logs/`

| Log File | Source | Content |
|---|---|---|
| `orchestrator.log` | Orchestrator daemon | Agent spawn/stop, GPU lock, pending message scan |
| `shared_listener.log` | Shared Listener | Telegram polling, IMAP fetch, message relay |
| `dashboard.log` | Dashboard (FastAPI) | HTTP requests, wizard actions, API calls |
| `{agent_name}.log` | Individual agents | LLM calls, tool execution, memory operations |
| `voice_listener.log` | Voice Listener (optional) | VAD events, Whisper transcription, TTS output |

---

## 12. Troubleshooting

### 12.1 Agent Does Not Start

| Symptom | Cause | Solution |
|---|---|---|
| No agent spawn after Telegram message | Orchestrator disabled | Check dashboard: orchestrator must show "enabled". Or run: `python3 -c "..."` from start_clean.sh |
| Agent spawns but crashes immediately | Missing secrets (Telegram token, API keys) | Check agent config in wizard. Verify `allowed_chat_ids` contains your Telegram user ID |
| "already running" but agent is dead | Stale PID file | Remove: `rm /tmp/aimos_agent_{name}.pid` and restart |
| Spawn backoff active | Repeated failures triggered CR-187 backoff | Check `logs/orchestrator.log` for backoff messages. Fix root cause, then restart orchestrator |

### 12.2 LLM / VRAM Issues

| Symptom | Cause | Solution |
|---|---|---|
| Agent responds very slowly (>60s) | Model loading from disk each time | Increase `LLM_KEEP_ALIVE` in `.env` (default: 30m). Check that only one agent runs at a time |
| OOM error in logs | VRAM exhausted | Reduce `LLM_NUM_CTX` (default: 14336). Check for zombie Ollama processes: `ollama ps` |
| CJK characters in response | Model language drift | Verify `CLEAN_CJK=true` in `.env`. Output firewall should strip automatically |
| "Whisper failed" in voice messages | CUDA libs not found | Restart via `start_clean.sh` (sets LD_LIBRARY_PATH). Or set CUDA paths manually |

### 12.3 Database Issues

| Symptom | Cause | Solution |
|---|---|---|
| "DB connection failed" | PostgreSQL not running or wrong credentials | `sudo systemctl status postgresql` and verify `.env` credentials |
| "Missing tables" in validate_cr.py | First boot hasn't run yet | Start any agent once — `_ensure_schema()` creates all tables automatically |
| Messages stuck as unprocessed | Orchestrator not running or disabled | Check `ps aux | grep orchestrator`. Enable via dashboard |
| 12x reply bug | Stale messages replayed after restart | Fixed by CR-049. Use `start_clean.sh` which expires stale messages on boot |

### 12.4 Telegram Issues

| Symptom | Cause | Solution |
|---|---|---|
| Bot not responding | shared_listener not running | `ps aux | grep shared_listener`. Restart via `start_clean.sh` |
| 429 Too Many Requests | Telegram rate limit | Automatic: exponential backoff in listener (CR-165). Wait and retry |
| "Token invalid" in logs | Revoked or wrong bot token | Regenerate token via @BotFather. Update in agent wizard |
| Messages from unknown users ignored | Chat ID not in allowed list | Add user's chat_id to agent config `allowed_chat_ids`. Auto-bootstrap authorizes on first contact |

### 12.5 Dashboard Issues

| Symptom | Cause | Solution |
|---|---|---|
| Port 8080 not accessible | Dashboard not running or port conflict | `fuser 8080/tcp` to check. Kill conflicting process. Restart dashboard |
| 401 Unauthorized | Wrong password | Check `AIMOS_DASHBOARD_PASSWORD` in `.env`. Default: `aimos2026` (change in production) |
| GPU bar shows 0% | nvidia-smi not found or GPU polling failed | Verify `nvidia-smi` works from command line. Check dashboard.log |

---

## 13. Architecture Quick Reference

```
start_clean.sh
  |
  +-- Dashboard (FastAPI :8080)      # Web UI, wizard, agent management
  +-- Shared Listener                # Telegram + IMAP polling -> DB
  +-- Orchestrator (daemon)          # 2s scan loop
        |
        +-- Spawns: main.py --id {agent} --mode auto
              |
              +-- AIMOSAgent kernel
              |     +-- LLM (Ollama API, native tool-calling)
              |     +-- SKILL_REGISTRY (brave_search, email, file_ops, ...)
              |     +-- Memory (SQLite: memories, kv_store, vault_mappings)
              |     +-- Output Firewall (CJK, thought-leak filter)
              |
              +-- Connectors
                    +-- TelegramConnector (outbound via DB relay)
                    +-- IMAP/SMTP (via email skill)

Data flow:
  User -> Telegram -> shared_listener -> pending_messages (DB)
  Orchestrator detects pending -> spawns agent
  Agent reads message -> LLM thinks -> tool calls -> writes reply
  Reply -> pending_messages (outbound) -> shared_listener -> Telegram -> User
```

---

## 14. Security Checklist

- [ ] Change default dashboard password (`AIMOS_DASHBOARD_PASSWORD`)
- [ ] PostgreSQL listens on localhost only (`listen_addresses = 'localhost'` in postgresql.conf)
- [ ] Ollama listens on localhost only (default behavior)
- [ ] Firewall enabled (ufw) with only ports 22 and 8080 open
- [ ] `.env` file permissions: `chmod 600 .env`
- [ ] No secrets in git history (use `.gitignore` for `.env`)
- [ ] PII Vault enabled for agents handling personal data (CR-075)
- [ ] Dashboard behind reverse proxy (nginx/caddy) with TLS for production

---

*Runbook prepared for AIMOS v4.6.1. Last updated: 2026-03-25.*
