# AIMOS Tool-Phase-Mapping — OODA Compliance

**Stand:** 03. April 2026
**Zweck:** Definiert welche Tools in welcher OODA-Phase aufgerufen werden dürfen.
Basis für statische Prüfung bei Agent-Deployment.

---

## OODA-Phasen Rekapitulation

```
Phase 0: KONTEXT    — Workspace laden, Datenquellen prüfen, Vorwissen aktivieren
Phase 1: OBSERVE    — Inputs strukturieren (Messages + Dokument-Inventory)
Phase 2: ORIENT     — Lagebild bauen, Zusammenhänge erkennen
Phase 2b: DECIDE    — Stakeholder identifizieren, Verarbeitungsplan erstellen
Phase 3c: ANALYSE   — Dokumente chunk-weise lesen und kategorisieren (NEU: VOR Phase 3)
Phase 3: ACT        — Stakeholder antworten, Emails senden, Aufgaben delegieren
Phase 3b: VALIDATE  — Antworten auf Korrektheit prüfen
Phase 4: PERSIST    — Workspace-Dateien schreiben, Erinnerungen setzen, state.md
```

---

## Tool-Kategorien mit Phase-Zuordnung

### R: READ — Informationen beschaffen
Erlaubt in: Phase 0, 1, 2, 2b, 3c

| Tool | Skill | Beschreibung | Phasen |
|------|-------|-------------|--------|
| read_file | file_ops | Workspace-Datei lesen | 0, 1, 2, 3c, 4 |
| read_file_chunked | file_ops | Große Datei seitenweise lesen | 0, 3c |
| list_workspace | file_ops | Dateien im Workspace auflisten | 0, 1 |
| read_public | file_ops | Öffentliche Datei lesen (Norm, KB) | 0, 2 |
| search_in_file | file_ops | In Datei suchen | 0, 2, 3c |
| recall | persistence | Langzeitgedächtnis abfragen | 0, 2, 3 |
| current_time | system | Aktuelle Uhrzeit/Datum | 0, 1, 2, 3, 4 |
| system_status | system | Ollama/GPU Status | 0 |
| ocr_extract_text | document_ocr | PDF/Bild → Text | 3c |
| ocr_extract_fields | document_ocr | Strukturierte Daten aus Dokument | 3c |
| ocr_list_scannable | document_ocr | Scanbare Dateien auflisten | 0, 1 |

### D: DATA — Externe Datenquellen abfragen
Erlaubt in: Phase 0, 2, 3c

| Tool | Skill | Beschreibung | Phasen |
|------|-------|-------------|--------|
| fetch_user_mail | mail_monitor | IMAP-Postfach abrufen | 0 |
| search_mail | mail_monitor | Im Mail-Archiv suchen | 0, 2 |
| read_mail | mail_monitor | Einzelne Mail lesen | 0, 2, 3c |
| web_search | web_search | Internet-Recherche | 2, 3c |
| dropbox_list_folder | dropbox | Dropbox-Ordner auflisten | 0 |
| dropbox_download_file | dropbox | Datei aus Dropbox laden | 0 |
| dropbox_sync_folder | dropbox | Ordner synchronisieren | 0 |
| cb_search_items | codebeamer | Requirements suchen | 0, 2 |
| cb_get_item | codebeamer | Requirement lesen | 2, 3c |
| cb_get_item_relations | codebeamer | Traceability prüfen | 2, 3c |
| get_customer_balance | eta_accounting | Kundensaldo | 2 |
| list_unpaid_invoices | eta_accounting | Offene Rechnungen | 2 |
| search_transactions | eta_accounting | Buchungen suchen | 2, 3c |
| analyze_image | hybrid_reasoning | Bild via externe Vision-API | 3c |

### W: WRITE — Informationen persistieren
Erlaubt in: Phase 3c (auto-persist), Phase 4

| Tool | Skill | Beschreibung | Phasen |
|------|-------|-------------|--------|
| write_file | file_ops | Workspace-Datei schreiben | 4 |
| remember | persistence | Fakt ins Langzeitgedächtnis | 4 |
| forget | persistence | Fakt aus Gedächtnis löschen | 4 |
| update_customer | contacts | Kundendaten aktualisieren | 4 |
| add_contact | contacts | Kontakt anlegen | 4 |

### C: COMMUNICATE — Nachrichten senden
Erlaubt in: Phase 3 (NACH Dokument-Analyse!)

| Tool | Skill | Beschreibung | Phasen |
|------|-------|-------------|--------|
| send_email | email | Email an Mandant/Stakeholder | 3 |
| send_telegram_message | system | Telegram-Nachricht | 3 |
| send_to_agent | system | An anderen Agent delegieren | 3 |
| teams_send_message | ms_teams | Teams-Nachricht | 3 |
| teams_create_meeting | ms_teams | Meeting erstellen | 3 |

### S: SCHEDULE — Zeitgesteuerte Aktionen
Erlaubt in: Phase 4

| Tool | Skill | Beschreibung | Phasen |
|------|-------|-------------|--------|
| set_reminder | scheduler | Erinnerung setzen | 4 |
| add_event | calendar | Kalendereintrag | 4 |
| list_jobs | scheduler | Geplante Jobs anzeigen | 0, 4 |

### E: ELSTER/EXTERNAL — Spezialisierte externe Systeme
Erlaubt in: Phase 3 (nach Vollständigkeitsprüfung), Phase 4

| Tool | Skill | Beschreibung | Phasen |
|------|-------|-------------|--------|
| elster_build_declaration | elster | Steuererklärung bauen | 4 |
| elster_validate | elster | Erklärung validieren | 4 |
| elster_submit | elster | Erklärung einreichen | 3 (NUR nach Mandant-OK!) |
| ask_external | hybrid_reasoning | Externe LLM-API | 2 (nur wenn lokal nicht lösbar) |

---

## Verbotene Kombinationen

| Regel | Begründung |
|-------|-----------|
| **COMMUNICATE in Phase 0/1/2** | Agent darf nicht antworten bevor er das Lagebild hat |
| **COMMUNICATE in Phase 3c** | Agent darf nicht antworten bevor er die Dokumente gelesen hat |
| **WRITE in Phase 0/1/2/3** | Workspace-Dateien nur in Phase 4 schreiben (außer auto-persist in 3c) |
| **ELSTER_SUBMIT ohne Mandant-OK** | Niemals automatisch einreichen |
| **ask_external in Phase 3c** | Außer analyze_image — keine Cloud-Calls für Textanalyse |
| **web_search in Phase 3/4** | Recherche gehört in Phase 2/3c, nicht beim Antworten |

---

## OODA-Compliance Check (statisch)

Für jede Agent-Config prüfen:

```python
def check_ooda_compliance(agent_config: dict) -> list[str]:
    """Prüft ob die Agent-Skills zur OODA-Phase passen."""
    warnings = []
    skills = agent_config.get("skills", [])
    strategy = agent_config.get("execution_strategy", "reactive")
    
    if strategy != "batch":
        return []  # Nur für Worker/OODA-Agenten relevant
    
    # Pflicht-Skills für Worker
    if "file_ops" not in skills:
        warnings.append("CRITICAL: Worker ohne file_ops kann keine Workspace-Dateien lesen")
    if "persistence" not in skills:
        warnings.append("WARNING: Worker ohne persistence verliert Langzeitgedächtnis")
    
    # Kommunikation braucht mindestens einen Kanal
    comm_skills = {"email", "ms_teams"} & set(skills)
    if not comm_skills:
        warnings.append("WARNING: Worker ohne email/teams kann keine Stakeholder kontaktieren")
    
    # Dokument-Analyse braucht OCR wenn workspace_scan aktiv
    if agent_config.get("batch_workspace_scan") and "document_ocr" not in skills:
        warnings.append("CRITICAL: batch_workspace_scan=true aber document_ocr nicht in Skills")
    
    return warnings
```

---

## Agenten-Compliance-Matrix

| Agent | R | D | W | C | S | E | Compliant? |
|-------|---|---|---|---|---|---|-----------|
| **steuerberater** | file_ops, ocr | mail, dropbox, web | file_ops, persist | email | scheduler, cal | elster | ✅ |
| **fusa_manager** | file_ops, ocr | codebeamer, web | file_ops, persist | email, teams | scheduler, cal | — | ✅ |
| **handwerker** | file_ops | — | file_ops, persist | email | scheduler, cal | — | ✅ |
| **req_manager** | file_ops | codebeamer | file_ops, persist | email | scheduler | — | ✅ |
| **bauer_support** | file_ops | — | persist, contacts | email, telegram | — | — | ✅ (reactive, kein OODA) |
