# AIMOS Agent Interface Protocol — v4.1.0

## 1. How the Orchestrator Calls an Agent

```
python main.py --id <agent_name> --mode <manual|orchestrator>
```

**Environment Variables** (injected by orchestrator/dashboard):
- All keys from `global_settings` where key starts with `secret.`
- All keys from `agents.env_secrets` for this agent
- Agent secrets override global secrets override `.env` file

**The agent process must:**
1. Connect to PostgreSQL (connection params from `core.config.Config`)
2. Read its own config from `agents.config` (JSON)
3. Load only the modules listed in `config.modules`
4. Process messages and exit cleanly on SIGTERM

## 2. Message Format (pending_messages)

```sql
CREATE TABLE pending_messages (
    id          SERIAL PRIMARY KEY,
    agent_name  TEXT NOT NULL,       -- target agent
    sender_id   BIGINT,             -- source user ID (Telegram chat_id, 0 for system)
    content     TEXT NOT NULL,       -- message payload
    kind        TEXT DEFAULT 'text', -- source channel
    file_path   TEXT,               -- attachment path (if any)
    processed   BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
```

**Kind values** (source channel):
| Kind | Source | sender_id | Reply via |
|---|---|---|---|
| `telegram` | Telegram text message | chat_id | `telegram.send_message` |
| `telegram_voice` | Telegram voice note | chat_id | `telegram.send_message` |
| `telegram_doc` | Telegram document | chat_id | `telegram.send_message` |
| `email` | IMAP inbox | 0 | `email.send_email` tool |
| `dashboard` | Alfred sidebar | 0 | DB / Dashboard UI |
| `voice` | Local microphone | 0 | TTS speaker |

**Content format for email:**
```
[E-Mail empfangen]
Von: sender@example.com
Betreff: Subject line
Datum: Wed, 18 Mar 2026 10:00:00 +0100
Text: Email body...
Anhaenge im Workspace: document.pdf, image.png
```

## 3. Response Routing

The agent does NOT decide where to send replies. `main.py` routes based on `kind`:

```python
if "telegram" in kind and sender_id != 0:
    telegram.send_message(chat_id=sender_id, text=reply)
elif kind == "email":
    # Agent uses send_email tool internally
elif kind == "dashboard":
    # Reply logged to DB / returned via API
```

## 4. Agent Configuration (agents table)

```json
{
  "display_name": "Neo",
  "modules": ["telegram", "brave_search"],
  "execution_strategy": "sequential",
  "voice_mode": "off",
  "character": {
    "nature": "Schlagfertig und neugierig.",
    "humor": "Trocken.",
    "curiosity": "Fragt nach wenn unklar."
  },
  "system_prompt": ""
}
```

`modules` controls which tools are loaded at startup.
`system_prompt` overrides character-based prompt if non-empty.

## 5. Adding a New Agent

1. Create agent in DB via Dashboard Wizard (`/wizard`)
2. Set `modules` (which connectors/tools to load)
3. Set agent-specific secrets in `env_secrets` (Telegram token, email creds)
4. Agent is ready — Orchestrator or Dashboard can start it
