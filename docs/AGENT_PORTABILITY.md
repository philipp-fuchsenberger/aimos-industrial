# AIMOS Agent Portability — Standards Roadmap

**CR-121** | Status: **DONE** (v4.3.2) | Priority: HIGH

## Vision

Ein AIMOS-Agent ist eine **portable Einheit** die zwischen Systemen migriert werden kann —
mit vollem Gedächtnis, Skills, Persönlichkeit und Konversationshistorie. Kein Vendor Lock-in,
kein Datenverlust bei Migration.

---

## 1. Aktueller Stand (v4.3.6)

| Komponente | Format | Portabel? |
|---|---|---|
| Agent-Config | PostgreSQL JSONB (`agents.config`) | Teilweise — DB-abhängig |
| Langzeitgedächtnis | SQLite `memory.db` pro Agent | **Ja** — eine Datei |
| Skill-State | SQLite `skill_state` Tabelle | **Ja** — in memory.db |
| Chat-History | PostgreSQL `aimos_chat_histories` | Nein — zentrale DB |
| Skills | Python-Klassen (BaseSkill) | Nein — AIMOS-proprietär |
| Connectors | Hardcoded in shared_listener | Nein — AIMOS-proprietär |
| Tool-Calling | Ollama native API | Teilweise — Ollama-spezifisch |
| Agent-to-Agent | DB-Relay (`pending_messages`) | Nein — AIMOS-proprietär |

---

## 2. Zielarchitektur (Standards-konform)

### 2.1 MCP (Model Context Protocol) — Tool & Connector Layer

**Was:** Anthropic/Linux Foundation Standard für Agent↔Tool Kommunikation.
97 Mio. Downloads/Monat. Adoptiert von OpenAI, Google, Microsoft, Amazon.

**Umsetzung für AIMOS:**
- Jeder Skill wird ein **MCP-Server** (eigener Prozess/Endpoint)
- Agent kommuniziert mit Skills über MCP statt direktem Python-Call
- Connectors (Telegram, E-Mail, SFTP) als MCP-Ressourcen
- LLM-Anbindung über MCP statt direkter Ollama HTTP-Calls

**Vorteil:** Skills werden austauschbar zwischen Frameworks. Ein AIMOS-Skill
funktioniert auch in LangChain, CrewAI, AutoGen etc.

```
Vorher:  Agent → BaseSkill.execute_tool() → Python
Nachher: Agent → MCP Client → MCP Server (Skill) → Tool
```

### 2.2 A2A (Agent-to-Agent Protocol) — Inter-Agent Layer

**Was:** Google/Linux Foundation Standard für Agent↔Agent Kommunikation.
Discovery, Delegation, Collaboration.

**Umsetzung für AIMOS:**
- `send_to_agent` → A2A Task Delegation
- Agent Cards (JSON-LD) für Discovery: "Welche Agenten gibt es? Was können sie?"
- Standardisierte Task-Formate statt DB-Relay
- Agenten auf verschiedenen Servern können kommunizieren (nicht nur lokal)

**Vorteil:** AIMOS-Agenten können mit Agenten aus anderen Frameworks sprechen.

```
Vorher:  Agent → INSERT pending_messages → DB → Agent
Nachher: Agent → A2A Client → HTTPS → A2A Server (Agent) → Task
```

### 2.3 OSSA/ADL — Agent Definition Layer

**Was:** Vendor-neutrales YAML-Format für Agent-Beschreibungen.
"Define once, export anywhere."

**Umsetzung für AIMOS:**
- Agent-Config als OSSA-YAML exportierbar
- Enthält: Name, Personality, Skills, Connectors, Permissions, Language
- Import: OSSA-YAML → AIMOS agents.config
- Export: agents.config → OSSA-YAML

**Beispiel:**
```yaml
# kral.agent.yaml (OSSA-Format)
apiVersion: ossa/v1
kind: Agent
metadata:
  name: kral
  displayName: Kral
  language: tr
  version: "1.0"
spec:
  personality:
    description: "Şeker ailesinin dijital asistanı. Galatasaray fanatik taraftarı."
    style: delikanlı
    tone: respectful-but-casual
  capabilities:
    skills:
      - brave_search
      - file_ops
      - scheduler
      - football_observer
      - tr_calendar_awareness
      - persistence
    connectors:
      - telegram
      - email
      - remote_storage
    interAgent:
      enabled: true
      allowedAgents: []  # all
  memory:
    format: agent-file/v1
    tieredScoring: true
    consolidation: dreaming
  security:
    editableCredentials:
      - EMAIL_ADDRESS
      - EMAIL_PASSWORD
      - REMOTE_SFTP_HOST
```

### 2.4 Agent File (.af) — Memory & State Portability

**Was:** Letta/MemGPT Open Standard für stateful Agent-Serialisierung.
Ein Agent = eine Datei.

**Umsetzung für AIMOS:**
- Export: `python -m aimos.export --agent kral --output kral.af`
- Import: `python -m aimos.import --file kral.af`
- Inhalt der .af Datei:

```
kral.af (ZIP-Archiv):
├── manifest.json          # Agent-Metadaten, Version, Format
├── agent.yaml             # OSSA Agent-Definition
├── memory.db              # SQLite Langzeitgedächtnis (memories, skill_state)
├── history.jsonl          # Chat-History (JSONL, portabel)
├── personality.txt        # System-Prompt
└── skills/                # Skill-Konfigurationen
    ├── football_observer.json
    ├── tr_calendar.json
    └── persistence.json
```

**Vorteil:** Kral kann als `kral.af` exportiert, auf ein anderes System kopiert,
und dort mit vollem Gedächtnis weiterarbeiten. Wie ein USB-Stick für KI-Agenten.

---

## 3. Migration in der Praxis

### Szenario: Agent von AIMOS-Server A nach AIMOS-Server B migrieren

```
Server A:                          Server B:
  aimos export --agent kral          aimos import --file kral.af
       ↓                                 ↓
  kral.af (eine Datei)    ──FTP──→  kral.af
       ↓                                 ↓
  Agent gestoppt                    Agent gestartet
                                    (volles Gedächtnis,
                                     alle Skills,
                                     Persönlichkeit intact)
```

### Szenario: Agent von AIMOS zu anderem Framework migrieren

```
AIMOS:                             Anderes Framework:
  aimos export --agent kral          Import .af Datei
  --format ossa                      Personality → System Prompt
       ↓                            Memory → Framework Memory
  kral.agent.yaml                   Skills → MCP Server Referenzen
  kral.memory.jsonl                 History → Conversation Log
```

---

## 4. Implementierungs-Reihenfolge

| Phase | Was | Aufwand | Status |
|---|---|---|---|
| **Phase 1** | Agent File Export/Import (.oap) | 2-3 Tage | **DONE** — `scripts/agent_export.py` |
| **Phase 2** | OAMF v1.0 Memory Format | 1-2 Tage | **DONE** — `docs/OPEN_AGENT_MEMORY_FORMAT.md` |
| **Phase 3** | MCP Bridge (39 tools) | 1 Woche | **DONE** — `core/skills/mcp_bridge.py` |
| **Phase 4** | A2A Agent Cards (5 agents) | 1 Woche | **DONE** — `core/skills/mcp_bridge.py` |

All 4 phases implemented in v4.3.2. Full MCP/A2A runtime integration (separate processes) is future work.

---

## 5. Memory-Standard (AIMOS-Erweiterung)

Da kein etablierter Memory-Standard existiert (Agent File ist das näheste),
definiert AIMOS ein offenes Memory-Format:

### `memory.jsonl` — Portable Memory Format

```jsonl
{"key": "user_name", "value": "Ugur Şeker", "category": "semantic", "importance": 9, "access_count": 15, "source": "user", "created": "2026-03-21T10:04:00Z", "updated": "2026-03-21T10:52:00Z"}
{"key": "firma_branche", "value": "Aich Makalsan - Beton Makine İmalatı", "category": "semantic", "importance": 8, "access_count": 7, "source": "agent", "created": "2026-03-21T10:49:00Z"}
{"key": "gs_mac_20260321", "value": "Galatasaray 2-1 kazandı", "category": "episodic", "importance": 6, "access_count": 1, "source": "tool", "created": "2026-03-21T18:00:00Z"}
```

Jede Zeile = eine Erinnerung mit:
- Tiered Scoring (importance, access_count)
- Kategorien (semantic, episodic, procedural)
- Provenienz (user, agent, tool, auto)
- Timestamps für Recency-Berechnung

Dieses Format ist framework-agnostisch und kann in jedes Memory-System importiert werden.
