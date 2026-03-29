# AIMOS Agent Memory Architecture — v4.3.6

## Design Principles

Inspired by human cognitive architecture and current agent research (Park et al. 2023 —
"Generative Agents", LangChain Memory, MemGPT / Letta):

1. **Tiered Storage** — not all memories are equal
2. **Relevance-Based Retrieval** — not recency alone, but importance + recency + access frequency
3. **Automatic Consolidation** — working memory promotes to long-term on session end
4. **Graceful Decay** — unused memories fade, frequently accessed ones strengthen
5. **Bounded Context** — only the most relevant memories are injected into the LLM prompt

## Memory Tiers

```
┌─────────────────────────────────────────────────────────┐
│  Working Memory (LLM Context Window)                     │
│  - Last 50 messages (HISTORY_LIMIT)                      │
│  - Ephemeral, lost when agent restarts                   │
│  - Size: ~8K tokens (num_ctx)                            │
└──────────────────────┬──────────────────────────────────┘
                       │ auto-inject top-scored memories
┌──────────────────────▼──────────────────────────────────┐
│  Long-Term Memory (SQLite: memory.db)                    │
│                                                          │
│  ┌─────────────────────────────────────────────────┐     │
│  │ Semantic Memory (facts, knowledge)               │     │
│  │ category: personal | project | fact | preference │     │
│  │ "Hund heißt Milo", "Chef mag keinen Kaffee"     │     │
│  └─────────────────────────────────────────────────┘     │
│  ┌─────────────────────────────────────────────────┐     │
│  │ Episodic Memory (events, interactions)           │     │
│  │ category: event | interaction | observation      │     │
│  │ "Am 19.3. Strategie-Call mit Inhaber geführt"    │     │
│  └─────────────────────────────────────────────────┘     │
│  ┌─────────────────────────────────────────────────┐     │
│  │ Procedural Memory (learned patterns)             │     │
│  │ category: procedure | rule | preference          │     │
│  │ "User bevorzugt kurze Antworten auf Telegram"    │     │
│  └─────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────┘
```

## Schema: memories table (SQLite)

| Column | Type | Purpose |
|---|---|---|
| id | INTEGER PK | Auto-increment |
| key | TEXT UNIQUE | Short identifier (e.g. "hund_name") |
| value | TEXT | The actual memory content |
| category | TEXT | semantic / episodic / procedural |
| importance | INTEGER | 1-10 (10 = critical, 1 = trivial) |
| access_count | INTEGER | How often this memory was retrieved |
| last_accessed | TEXT | Timestamp of last retrieval |
| source | TEXT | "user" (told by user) / "self" (agent derived) / "tool" (from tool result) |
| created_at | TEXT | When first stored |
| updated_at | TEXT | When last modified |

## Relevance Scoring Formula

When selecting which memories to inject into the system prompt:

```
score = importance * recency_weight * (1 + ln(access_count + 1))

where:
  recency_weight = 1.0 / (1.0 + days_since_last_access * 0.1)
```

This means:
- **High importance** (10) always scores well
- **Recent access** boosts relevance (accessed today = 1.0, 10 days ago = 0.5)
- **Frequently accessed** memories get a logarithmic boost (diminishing returns)
- A memory with importance=10, accessed today, accessed 5 times:
  score = 10 * 1.0 * (1 + ln(6)) = 10 * 2.79 = 27.9
- A memory with importance=3, accessed 30 days ago, accessed once:
  score = 3 * 0.25 * (1 + ln(2)) = 3 * 0.25 * 1.69 = 1.27

## Context Budget

- Max memories injected per think() call depends on `cognitive_balance` setting:
  - cb=0 (Deep Memory): 50 memories — best for complex business/personal tasks
  - cb=1 (Recall): 35 memories
  - cb=2 (Balanced): 25 memories
  - cb=3 (Verbose): 15 memories
  - cb=4 (Eloquence): 8 memories — best for creative writing
- Sorted by relevance score descending
- Injected as `<langzeitgedaechtnis>` block in system prompt
- Each memory: `- [category] key: value (importance: X)`

## Hybrid Search — CR-140

Since v4.3.6, `recall()` uses hybrid search combining keyword matching and semantic vectors:

```
recall("Was macht Ugurs Firma?")
  │
  ├─ Path 1: FTS5 Keyword Search
  │   SQLite full-text index on key + value
  │   Falls back to LIKE if FTS5 MATCH syntax fails
  │
  ├─ Path 2: Vector Cosine Search
  │   all-MiniLM-L6-v2 (384 dims, CPU, ~10ms per query)
  │   Cosine similarity against all memory embeddings
  │
  └─ RRF Fusion (k=60)
      score = sum(1 / (60 + rank_keyword)) + sum(1 / (60 + rank_vector))
      Top-20 results returned
```

**Why this matters**: Keyword search fails when the query uses different words than the stored memory.
Example: Memory `firma_branche: "Aich Makalsan - Beton Makine İmalatı"` is found by
`recall("Firma Geschäft")` (cosine 0.65) but NOT by keyword search (no word overlap).

**Graceful fallback**: If `sentence-transformers` is not installed, vector search is skipped
and only FTS5 keyword search runs — still better than the old LIKE search.

### Embedding Storage

Each memory stores a 1,536-byte embedding BLOB (384 float32 values) alongside the text.
Embeddings are computed on `remember()` and backfilled for existing memories on agent startup.

## Agent Tools

| Tool | Purpose |
|---|---|
| `remember(key, value, category, importance)` | Store or update a memory (+ embedding + FTS5 index) |
| `recall(query)` | Hybrid search: FTS5 keyword + vector cosine + RRF fusion |
| `forget(key)` | Permanently delete a memory (+ FTS5 rebuild) |

## Workspace (Agent Filing System)

Agents are instructed to actively use their workspace folder for structured information:

- **Notes**: `write_file("notes/summary_topic.txt", ...)` — key findings from research, conversations
- **Todo lists**: `write_file("todo.txt", ...)` — open tasks, deadlines, status tracking
- **Large data**: When receiving search results or documents, extract key points into workspace notes

The workspace persists forever (unlike chat history which is compressed). Agents use `remember/recall`
for quick facts and their workspace for structured, longer-form information.

```
storage/agents/{name}/
├── memory.db          → SQLite (memories, skill_state, agent_log)
├── todo.txt           → Agent's running task list
├── notes/             → Organized notes by topic
│   ├── customer_X.txt
│   └── project_Y.txt
└── public/            → Cross-agent readable folder
```

## Automatic Behaviors

1. **Access Tracking**: Every `recall()` call increments `access_count` and updates `last_accessed`
2. **Auto-Inject**: `_load_memory_context()` runs at every `think()`, selecting top memories by score
3. **Deduplication**: Same key overwrites (UPSERT), preventing duplicates
4. **Embedding Backfill**: On agent startup, memories without embeddings get them automatically (~500ms for 50 memories)
5. **History Compression**: On agent startup, old tool results (>20 messages back) are truncated to 200 chars. Hard cap: 40 messages per agent. Prevents context overflow.
6. **Dreaming Extraction**: Idle agents use LLM to extract facts from conversations → permanent memories

## Emotional & Contextual Triggers in Memory

Agents can store emotional events as episodic memories with high importance, enabling
later contextual reference. Example flow (Agent "Kral", Galatasaray football tracker):

1. **Event detected**: `football_observer` skill fetches match result → "Galatasaray 3-1 win"
2. **Agent stores**: `remember(key="gs_mac_20260320", value="Galatasaray 3-1 kazandı! Büyük zafer!", category="episodic", importance=8)`
3. **Mood stored**: `remember(key="gs_ruh_hali", value="ZAFER — coşkulu", category="procedural", importance=6)`
4. **Later reference**: When user messages the agent days later, the memory is auto-injected
   into the system prompt. Agent can say: "Abi, hatırlıyor musun geçen hafta nasıl coştuk? 3-1!"

Similarly for cultural awareness (Turkish calendar):
1. **Holiday detected**: `tr_calendar_awareness` skill → "29 Ekim Cumhuriyet Bayramı"
2. **Agent stores**: `remember(key="bayram_29ekim_2026", value="Cumhuriyet Bayramı kutlandı", category="episodic", importance=7)`
3. **Greeting injected**: Agent starts conversation with festive greeting on that day
4. **Future recall**: "Abi, geçen Cumhuriyet Bayramı'nda ne güzel kutlamıştık!"

This pattern — skill detects event → agent stores in episodic memory → auto-inject on recall —
works for any domain-specific trigger (sports, holidays, market events, deadlines).

## Dreaming (Memory Consolidation + Extraction)

When an agent is idle for >= 1 hour, the Orchestrator triggers a "dream" cycle.
The agent reviews its recent conversations, extracts important facts into long-term
memory, and then consolidates and cleans up its memory database.

### Why Dreaming Matters

During active conversations, agents focus on responding quickly. They may miss
storing important facts — corrections, preferences, decisions, new information.
Dreaming is the agent's opportunity to **reflect on what happened** and build
lasting memories from ephemeral conversations.

### Trigger

- Orchestrator checks every ~60s (`cycle % 30`)
- **Context pressure**: agent has >25 messages in chat history (not idle time)
- An agent that has nothing to process doesn't dream — no GPU waste
- Each agent dreams once per buildup; resets when history drops below threshold
  (after `_compress_history` cleans up at next startup)
- Only active-team agents dream — inactive agents are completely off
- Dashboard shows purple "DREAMING" badge during dream cycles
- CPU priority lowered via `os.nice(10)` during dream

### What Was Removed

- **`_auto_remember` (regex safety net)**: Previously, keywords like "merk dir", "remember",
  "unutma" triggered automatic memory storage from chat messages. This caused false positives
  and stored raw message text without understanding context. Replaced entirely by Dreaming
  Phase 0 which uses the LLM to understand what's worth remembering.

### Dream Phases

```
core/dreaming.py → dream(agent_name, db_path)
  │
  ├── Phase 0: _extract_facts_from_history()     ← LLM-POWERED
  │   Loads recent conversation from PostgreSQL
  │   Sends to local LLM with extraction prompt:
  │     "Analyze this conversation. Extract all facts worth
  │      remembering: corrections, preferences, decisions, names,
  │      relationships. Return as JSON lines."
  │   LLM returns two types of output:
  │     MEM:{key, value, category, importance} → stored as memories
  │     FILE:{path, content} → written to agent workspace
  │   Examples: MEM: facts, corrections, preferences
  │             FILE: notes/customer_X.txt, todo.txt updates
  │   Timestamps tracked — only processes new messages since last dream
  │   LLM sees existing workspace files + memories (no duplicates)
  │   Language: prompt is English (system standard), extraction in
  │   conversation language (Turkish facts stay Turkish, etc.)
  │
  ├── Phase 1: _consolidate_similar()
  │   Jaccard similarity >= 0.6 on tokenized key+value within same category
  │   → Merge: winner gets max(importance)+1, summed access_counts; loser deleted
  │
  ├── Phase 2: _resolve_contradictions()
  │   Same key-prefix (before first '_') AND Jaccard < 0.2 on values
  │   → Keep higher access_count (tie: newer); delete loser
  │
  ├── Phase 3: _decay_stale()
  │   30d+ unused, importance <= 4 → importance - 1
  │   90d+ unused, importance <= 2 → DELETE
  │
  └── Phase 4: _clean_hallucinations()
      Heuristics: value < 5 chars, only symbols, known garbage ("None", "ok", ...),
      repeated chars (5+), unreinforced self-generated noise (7d+)
      Safety: importance >= 7 OR access_count >= 3 → NEVER deleted
```

### Safety Guarantees

- **No data loss for important memories**: importance >= 7 or access_count >= 3 are protected
- **Gradual consolidation**: only one merge pass per dream — avoids aggressive bulk changes
- **Phase 0 uses local LLM**: one inference call on the local GPU (~2K input, ~500 output tokens). No external API, no data leaves the server. Temperature 0.1 for deterministic extraction.
- **Phases 1-4 are LLM-free**: pure deterministic string matching on SQLite
- **Idempotent**: extraction timestamps are tracked — same messages are never re-processed
- **Low priority**: `os.nice(10)` ensures dreaming doesn't compete with active agents
- **GPU availability**: dreaming only runs when agent is idle >1h, meaning the GPU is free (no VRAM conflict)

### Dream Log

After each dream, a summary is written to the `agent_log` table:
```
Dream complete: 12 merged, 3 contradictions resolved, 5 decayed, 2 hallucinations cleaned (47ms)
```

## Storage Location

```
storage/agents/{name}/memory.db
  ├── memories          (long-term memory with scoring)
  ├── skill_state       (per-skill persistent state)
  ├── agent_log         (private logs)
  └── vault_mappings    (PII anonymization sessions)
```
