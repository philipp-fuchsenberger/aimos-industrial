# AIMOS OODA-Zyklus — Vollständiges Phasenmodell

**Stand:** 03. April 2026
**Version:** v4 (6 Phasen, Tool-Filtering + Draft→Validate→Dispatch implementiert)

---

## Übersicht

```
Phase 0       Phase 1       Phase 2              Phase 3       Phase 4              Phase 5
KONTEXT  →    OBSERVE  →    ORIENT          →    DECIDE  →     ACT            →     PERSIST
1 Call        1 Call        1 + N Calls (Loop)   1 Call        N × (Draft+Val)      1 Call
39 Tools      5 Tools       44 Tools             1 Tool        14 Tools             40 Tools
R + D         R             R + D + W + E        R             R + W                R + W + S + E

                                                               4c: DISPATCH
                                                               Orchestrator (7 Tools)
                                                               KEIN LLM-Call!
```

---

## Phase 0: KONTEXT

**Zweck:** Arbeitsstand laden, Datenquellen synchronisieren, Vorwissen aktivieren
**Calls:** 1 LLM-Call
**Input:** — (Start des Zyklus)
**Output:** phase0_context → Phase 1

### Vorgänge

| # | Vorgang | Tools |
|---|---------|-------|
| 0.1 | Workspace-State laden (state.md lesen) | read_file |
| 0.2 | Dropbox/SharePoint/IMAP synchronisieren | dropbox_sync_folder, fetch_user_mail, sp_list_folder |
| 0.3 | Überfällige Aufgaben + Termine prüfen | check_overdue, check_today, list_events |
| 0.4 | Workspace auf neue Dateien scannen | list_workspace, ocr_list_scannable |
| 0.5 | Überlast-Prüfung (>200 Dateien → Abbruch) | list_workspace |

**39 Tools:** 13 READ + 24 DATA + 1 SCHEDULE + 1 EXTERNAL
**Blocked:** WRITE, COMMUNICATE

---

## Phase 1: OBSERVE

**Zweck:** Alle Inputs strukturieren, gruppieren, priorisieren
**Calls:** 1 LLM-Call
**Input:** phase0_context + Messages + Dokument-Inventory
**Output:** analysis → Phase 2

### Vorgänge

| # | Vorgang | Tools |
|---|---------|-------|
| 1.1 | Messages nach Absender/Thread gruppieren | — (Denkaufgabe) |
| 1.2 | Neue Dokumente inventarisieren | list_workspace, get_file_overview |
| 1.3 | Spam/Newsletter erkennen | — (Denkaufgabe) |
| 1.4 | Prioritäten setzen | current_time |

**5 Tools:** 5 READ
**Blocked:** DATA, WRITE, SCHEDULE, EXTERNAL, COMMUNICATE
**Begründung:** Kein Zugriff auf externe Quellen — nur Strukturierung dessen was Phase 0 geliefert hat.

---

## Phase 2: ORIENT (mit Loop)

**Zweck:** Lagebild bauen — vollständiges Verständnis ALLER Inputs
**Calls:** 1 + N LLM-Calls (N = Dokument-Chunks)
**Input:** analysis
**Output:** lagebild → Phase 3

### Substep 2a: Dokument-Chunk-Loop ⚠

| # | Vorgang | Tools |
|---|---------|-------|
| 2a.1 | OCR/Text-Extraktion (automatisch vor LLM) | ocr_extract_text, ocr_extract_fields |
| 2a.2 | Chunk analysieren + kategorisieren | web_search (bei unbekannten Einträgen) |
| 2a.3 | Ergebnis in arbeitsdatei.md schreiben | store_chunk_summary (auto-persist) |
| 2a.4 | History-Reset zwischen Chunks | — (Orchestrator-Code, CR-270) |

**Sicherungen:** max_chunks_per_cycle (20), Hard-Timeout (600s/Call), max_orient_duration (1800s geplant)

### Substep 2b: Lagebild-Konsolidierung

| # | Vorgang | Tools |
|---|---------|-------|
| 2b.1 | Cross-Referencing Messages ↔ Dokumente | read_file, recall |
| 2b.2 | Zusammenhänge erkennen | search_in_file, web_search |
| 2b.3 | Informationsdefizite identifizieren | — (Denkaufgabe) |
| 2b.4 | Lagebild als Text formulieren | — (LLM-Output) |

**44 Tools:** 13 READ + 27 DATA + 1 WRITE + 3 EXTERNAL
**Blocked:** SCHEDULE, COMMUNICATE
**Begründung:** Voller Recherche-Zugriff — der Agent muss alles wissen bevor er entscheidet.

---

## Phase 3: DECIDE

**Zweck:** Entscheiden WER WAS braucht
**Calls:** 1 LLM-Call
**Input:** lagebild
**Output:** stakeholder_plan → Phase 4

### Vorgänge

| # | Vorgang | Tools |
|---|---------|-------|
| 3.1 | Reaktive Stakeholder (wer hat geschrieben?) | — |
| 3.2 | Proaktive Stakeholder (wer ist betroffen?) | — |
| 3.3 | Pro Stakeholder: Was muss er wissen? | — |
| 3.4 | Reihenfolge nach Priorität/Impact | — |
| 3.5 | Dokument-Generierung nötig? (PDF, Aufstellung) | — |

**1 Tool:** current_time
**Blocked:** DATA, WRITE, SCHEDULE, EXTERNAL, COMMUNICATE
**Begründung:** Reine Denkphase. Alle Informationen sind im Lagebild. Keine Tools nötig.

---

## Phase 4: ACT (mit Loop + Validate + Dispatch)

**Zweck:** Stakeholder-Antworten generieren, Dokumente erstellen, versenden
**Calls:** N × (1 Draft + 1 Validate) LLM-Calls + Orchestrator-Dispatch
**Input:** lagebild + stakeholder_plan
**Output:** phase4_results → Phase 5

### Substep 4a: DRAFT generieren (pro Stakeholder)

| # | Vorgang | Tools |
|---|---------|-------|
| 4a.1 | Thread-History + Kontaktdaten laden | recall, lookup_thread, find_contact |
| 4a.2 | Draft-Antwort als Fließtext schreiben | — (LLM-Output) |
| 4a.3 | Optional: PDF/Excel-Aufstellung generieren | create_pdf, create_excel_sheet, write_file |
| 4a.4 | History-Reset zwischen Stakeholdern | — (CR-270) |

**14 Tools:** 5 READ + 9 WRITE
**Blocked:** DATA, SCHEDULE, EXTERNAL
**Nicht verfügbar:** send_email, teams_send_message (COMMUNICATE — Orchestrator-only)

### Substep 4b: VALIDATE (pro Draft)

| # | Vorgang | Tools |
|---|---------|-------|
| 4b.1 | Draft gegen Lagebild prüfen | — |
| 4b.2 | Cross-Thread-Leak prüfen | — |
| 4b.3 | Halluzinationen prüfen | — |
| 4b.4 | Ergebnis: APPROVED oder REVISE | — |

**1 Tool:** current_time
**Begründung:** Reine Prüfphase — keine Tools nötig.

### Substep 4c: DISPATCH (Orchestrator, KEIN LLM!)

| # | Vorgang | Orchestrator-Tool |
|---|---------|-------------------|
| 4c.1 | Wenn APPROVED: dispatch_response() | send_email |
| 4c.2 | Email mit Anhang (PDF/Aufstellung) | send_email + attachment |
| 4c.3 | Teams-Nachricht | teams_send_message |
| 4c.4 | Telegram | send_telegram_message |
| 4c.5 | An anderen Agent delegieren | send_to_agent |

**7 Orchestrator-Tools:** send_email, send_telegram_message, send_telegram_file, send_voice_message, send_to_agent, teams_send_message, teams_create_meeting
**KEIN LLM-Call.** Python-Code im Orchestrator (batch.py → dispatch_response()).
**Das LLM hat diese Tools NIEMALS gesehen.**

---

## Phase 5: PERSIST (Guaranteed Self-Dispatch)

**Zweck:** Interner State sichern — "Dispatch an sich selbst"
**Calls:** 1 LLM-Call
**Input:** lagebild + phase4_results + doc_results
**Output:** state.md, arbeitsdatei.md, todo.md, status.md
**Garantie:** Läuft IMMER, auch wenn Phase 4 fehlschlägt (finally-Block)
**Config:** `batch_persist: false` → Phase 5 überspringen (stateless Agent)

### Vorgänge

| # | Vorgang | Tools |
|---|---------|-------|
| 5.1 | arbeitsdatei.md LESEN (Pflicht!) | read_file |
| 5.2 | state.md schreiben | write_file |
| 5.3 | todo.md aktualisieren | write_file |
| 5.4 | status.md aktualisieren | write_file |
| 5.5 | Key-Facts ins Langzeitgedächtnis | remember |
| 5.6 | Fristen + Follow-ups setzen | set_reminder, add_event |
| 5.7 | Optional: ELSTER bauen/validieren | elster_build_declaration, elster_validate |
| 5.8 | Backup state.md.bak | — (Orchestrator-Code) |

**40 Tools:** 3 READ + 26 WRITE + 8 SCHEDULE + 3 EXTERNAL
**Blocked:** DATA, COMMUNICATE
**Begründung:** Voller Schreibzugriff — hier werden alle Ergebnisse gesichert.

---

## Zusammenfassung

| Phase | Name | Calls | Tools | Kategorien | Loop? |
|-------|------|-------|-------|-----------|-------|
| 0 | KONTEXT | 1 | 39 | R + D + S + E | Nein |
| 1 | OBSERVE | 1 | 5 | R | Nein |
| 2 | ORIENT | 1+N | 44 | R + D + W + E | **Ja** (Chunks) |
| 3 | DECIDE | 1 | 1 | R | Nein |
| 4 | ACT | N×2 | 14 | R + W | **Ja** (Stakeholder) |
| 5 | PERSIST | 1 | 40 | R + W + S + E | Nein |
| — | DISPATCH | 0 | 7 | C (Orchestrator) | — |

### Architektur-Prinzip

```
LLM:          DENKT + SCHREIBT Drafts + generiert Dokumente
Orchestrator: DISPATCHT nach außen (Outbound) + sichert intern (Self)

Phase 4 ACT:     Outbound-Dispatch (Email, Teams, JIRA, ELSTER...)
                 → Kann fehlschlagen, wird validiert, kann übersprungen werden
Phase 5 PERSIST: Self-Dispatch (state.md, Gedächtnis, Termine)
                 → Guaranteed, non-optional, läuft im finally-Block

Das LLM hat KEINEN Zugriff auf Dispatch-Tools (weder Outbound noch Self).
In ACT generiert es Drafts + Dokumente, der Orchestrator dispatcht.
In PERSIST schreibt es state.md, der Orchestrator sichert das Backup.
→ Eliminiert Phantom-Actions, JSON-in-Emails, Format-Probleme by design.
```

### Implementierung (CR-273, v4)

**Tool-Filtering** — Zweischichtige Absicherung:
1. **Prompt-Level:** `filter_tools_for_phase(ollama_tools, phase)` filtert die Tool-Liste
   vor jedem LLM-Call. Das LLM sieht nur die Tools seiner Phase.
2. **Execution-Level:** `_execute_tool()` prüft `is_allowed_in_phase(name, phase)` und
   blockt den Call mit Fehlermeldung, selbst wenn das LLM den Toolnamen "auswendig" kennt.

**Draft→Validate→Dispatch** — Phase 4 ACT:
1. LLM-Prompt fordert explizit: "Write as PLAIN TEXT, do NOT use send_email"
2. Tool-Filter entfernt COMMUNICATE-Tools (send_email, teams_send_message etc.)
3. Execution-Guard blockt ORCHESTRATOR_DISPATCH_TOOLS mit klarer Fehlermeldung
4. LLM darf weiterhin Anhänge erstellen (create_pdf, write_file — Kategorie W in Phase 4)
5. Nach Validate: `dispatch_response()` übernimmt den Versand (Orchestrator)

**Phase-Parameter-Kette:**
```
process_batch() → _think_with_activity_check(phase="X")
                  → agent._ooda_phase = "X"
                  → agent.think() → filter_tools_for_phase(tools, "X")
                                   → _execute_tool() → is_allowed_in_phase(name, "X")
```

### Confidentiality Scopes (CR-274)

Wenn ein Agent mehrere unabhängige Vorgänge gleichzeitig bearbeitet (z.B. Steuerberater
mit 2 Mandanten), schützt das Scope-System vor Datenlecks:

**3 Modi (`batch_confidentiality`):**
- `"none"` (Default): Ein Lagebild für alles. Prompt-basierte Isolation. Für FuSa, Teams.
- `"isolated"`: Lagebild partitioniert pro Scope. Phase 4 sieht NUR eigenen Scope.
  Deterministisch — LLM kann nicht leaken, weil andere Daten nicht im Kontext sind.
- `"tagged"`: Ein Lagebild mit Scope-Tags. Stärkere Validation.

**Scope-Resolver:** `_resolve_scope(thread_id, config)` — reines Python, kein LLM-Call.
Mappt `email:foo@bar.com` → `scope:foo@bar.com` per Config (`batch_scope_pattern`).

**Partitionierung:**
1. Phase 2 ORIENT baut Lagebild mit `## [SCOPE: ...]` Headers
2. `_partition_lagebild()` splittet per Regex
3. Phase 4 ACT bekommt nur `lagebild_partitions[scope]`

**Steuerberater:** `batch_confidentiality: "isolated"` — Mandantendaten sind strikt getrennt.
**FuSa Manager:** `batch_confidentiality: "none"` — Projekte dürfen sich gegenseitig sehen.
