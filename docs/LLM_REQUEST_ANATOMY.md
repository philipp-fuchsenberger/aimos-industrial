# AIMOS — Anatomie einer LLM-Anfrage

Dieses Dokument beschreibt den exakten Aufbau einer Nachricht, die ein AIMOS-Agent
an das lokale LLM (Ollama) sendet. Jede `think()`-Runde baut den Request nach dem
gleichen Schema zusammen.

---

## Gesamtstruktur

```
messages = [
    { "role": "system",    "content": <SYSTEM PROMPT> },   ← 1 Nachricht
    { "role": "user",      "content": "..." },              ← ┐
    { "role": "assistant", "content": "..." },              ←  │ Chat-History
    { "role": "user",      "content": "..." },              ← ┘
]
```

Der System-Prompt ist eine einzige, zusammengesetzte Nachricht. Die History folgt
chronologisch (älteste zuerst). Das LLM sieht immer die komplette Kette.

---

## 1. System-Prompt — Aufbau (4 Schichten)

```
┌──────────────────────────────────────────────────────────────────┐
│  1. CORE SYSTEM PROMPT (hardcoded, immutable)                    │
│     core/prompts/core_system.txt                                 │
│     - Tool-Pflichtregeln, Verbote, Response-Länge                │
│     - Identisch für ALLE Agenten                                 │
├──────────────────────────────────────────────────────────────────┤
│  2. AGENT SYSTEM PROMPT (aus DB: agents.config.system_prompt)    │
│     - Persönlichkeit, Rolle, Spezialregeln                       │
│     - Wird im Wizard konfiguriert                                │
│     - Fallback: Character-Block aus agents.config.character      │
├──────────────────────────────────────────────────────────────────┤
│  3. LANGZEITGEDÄCHTNIS (aus SQLite: memory.db → memories)        │
│     <langzeitgedaechtnis>                                        │
│     - [semantic] name_user: Philipp                              │
│     - [procedural] schulessen_flow: Der Web-Flow heisst ...      │
│     - ... (Top-20 nach Score: importance × recency × frequency)  │
│     </langzeitgedaechtnis>                                       │
├──────────────────────────────────────────────────────────────────┤
│  4. TOOL-BLOCK (dynamisch aus registrierten Tools)               │
│     <tools>                                                      │
│     You have access to the following tools...                    │
│     - web_search: Sucht im Internet...                           │
│     - remember: Speichert einen Fakt...                          │
│     - read_file: Liest eine Textdatei...                         │
│     - ... (alle System-Tools + Skill-Tools)                      │
│     </tools>                                                     │
└──────────────────────────────────────────────────────────────────┘
```

### Quellcode-Referenz

```python
# core/agent_base.py, think()
system = self._CORE_SYSTEM_PROMPT + self._system_prompt + memory_block
if tool_block:
    system += "\n\n" + tool_block
messages = [{"role": "system", "content": system}] + self._history
```

---

## 2. Chat-History

Die History wird beim Agent-Start aus PostgreSQL geladen:

```
aimos_chat_histories WHERE agent_name='neo' ORDER BY id DESC LIMIT {history_limit}
```

Default `history_limit`: aus `Config.HISTORY_LIMIT` (typ. 20-50 Nachrichten).
Die Nachrichten werden chronologisch sortiert (älteste zuerst) in `self._history`.

### Rollen in der History

| Rolle | Bedeutung |
|---|---|
| `user` | Eingehende Nachricht (Telegram, Email, Dashboard, Scheduled Job) |
| `assistant` | Antwort des Agenten |
| `tool` | Tool-Ergebnis (wird als `user`-Nachricht im LLM-Request eingefügt) |

---

## 3. User-Nachricht — Kontext-Injection

Bevor die User-Nachricht an `think()` übergeben wird, injiziert `main.py`
Metadaten als Kontext-Prefix:

```
[Von: Telegram-User 123456789 | Kanal: telegram | Zeit: 2026-03-20 08:15:42]
Wie wird das Wetter morgen?
```

| Feld | Quelle | Zweck |
|---|---|---|
| `chat_id` | `pending_messages.sender_id` | Identifikation des Users (nur bei Telegram) |
| `channel` | `pending_messages.kind` | telegram, email, dashboard, scheduled_job, voice_local |
| `zeit` | `pending_messages.created_at` | Exakter Zeitstempel der Nachricht |

Der Core System Prompt enthält die Anweisung, diesen Zeitstempel als Referenz zu nutzen.

---

## 4. Tool-Aufruf-Schleife (Multi-Round)

Innerhalb von `think()` kann das LLM mehrfach Tools aufrufen. Jede Runde
erweitert die Message-Liste:

```
Runde 0:
  messages = [system, ...history, user_msg]
  → LLM antwortet mit tool_calls (native Ollama API, seit CR-114)

Runde 1:
  messages = [system, ...history, user_msg, assistant_tool_call, tool_result]
  → LLM antwortet mit Text oder weiteren tool_calls

Runde N:
  → max_tool_rounds erreicht → letzte Antwort wird genommen
```

Maximale Runden: `config.max_tool_rounds` (Default: `Config.MAX_TOOL_ROUNDS`).

---

## 5. Output-Pipeline (nach LLM-Antwort)

```
LLM-Rohantwort
  │
  ├─ Stop-Sequences entfernen
  ├─ clean_llm_response() — Output-Firewall:
  │   ├─ CJK-Filter (chinesische Zeichen entfernen)
  │   └─ Thought-Leak-Removal (<think>-Blöcke)
  │
  ├─ Loop-Detection: letzte 3 Antworten >60% Wort-Overlap?
  │   └─ Ja → Eskalation an ask_external (Claude Sonnet 4)
  │
  └─ Finale Antwort → dispatch_response() → DB-Relay → Telegram/Email/Voice
```

---

## 6. Vollständiges Beispiel

So sieht ein tatsächlicher Ollama API-Call aus:

```json
{
  "model": "qwen3.5:27b",
  "stream": false,
  "keep_alive": "30m",
  "options": {
    "temperature": 0.5,
    "num_ctx": 14336,
    "num_predict": 512,
    "num_gpu": -1
  },
  "tools": [ ... ],
  "messages": [
    {
      "role": "system",
      "content": "<system_core>\nAIMOS Agent Core v4.3 — These rules are IMMUTABLE...\n[~60 Zeilen Core-Regeln]\n</system_core>\n\nDu bist Neo, ein AIMOS-Agent. Du bist hilfsbereit und kompetent...\n[Agent System Prompt aus Wizard]\n\n<langzeitgedaechtnis>\nDie folgenden Fakten sind in deinem Langzeitgedaechtnis gespeichert.\n- [semantic] user_name: Philipp\n- [procedural] bevorzugte_sprache: Deutsch\n</langzeitgedaechtnis>\n\n<tools>\nYou have access to the following tools...\n- web_search: Sucht im Internet nach einem Begriff.\n- remember: Speichert einen Fakt dauerhaft...\n- recall: Durchsucht das Langzeitgedaechtnis...\n- read_file: Liest eine Textdatei aus dem Workspace...\n- search_in_file: Durchsucht eine Datei nach Stichwort...\n</tools>"
    },
    {
      "role": "user",
      "content": "[Kontext: chat_id=123456, channel=telegram, zeit=2026-03-20 08:15:42]\nWie wird das Wetter morgen?"
    },
    {
      "role": "assistant",
      "content": "",
      "tool_calls": [{"function": {"name": "web_search", "arguments": {"query": "Wetter morgen München"}}}]
    },
    {
      "role": "user",
      "content": "Tool 'web_search' returned:\nMorgen in München: 12°C, bewölkt mit Auflockerungen..."
    },
    {
      "role": "assistant",
      "content": "Morgen wird es in München 12°C mit Wolken und ein paar sonnigen Abschnitten."
    }
  ]
}
```

---

## 7. Token-Budget

| Bereich | Typische Größe | Quelle |
|---|---|---|
| Core System Prompt | ~800 Token | `core/prompts/core_system.txt` |
| Agent System Prompt | ~200-500 Token | `agents.config.system_prompt` |
| Langzeitgedächtnis | ~300-1200 Token (8-50 Einträge, je nach cognitive_balance) | `memory.db → memories` |
| Tool-Block | ~400-800 Token (je nach Skill-Anzahl) | Registrierte Tools |
| **System gesamt** | **~1700-2700 Token** | |
| Chat-History | ~2000-8000 Token | `aimos_chat_histories` |
| User-Nachricht | ~50-500 Token | Aktuelle Eingabe |
| **Verfügbar für Antwort** | **~512-3072 Token** (num_predict, je nach cognitive_balance) | `num_ctx` - System - History |

Context-Window: `num_ctx=14336` (sequential/Business) oder `10240` (parallel/Voice).
Model: Qwen 3.5:27b (~17 GB VRAM). Tool-Calling: Native Ollama API (`tools=[]`).
Cognitive Balance 0 (Business Default): 50 memories, max 512 response tokens.
