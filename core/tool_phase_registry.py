"""
AIMOS Tool-Phase Registry — Definiert welche Tools in welcher OODA-Phase erlaubt sind.
======================================================================================

Jedes Tool MUSS hier registriert sein. Neue Tools ohne Registrierung werden
beim Agent-Start mit WARNING geloggt und in Phase 3 (ACT) blockiert.

Phasen:
  0  = KONTEXT (Workspace laden, Datenquellen prüfen)
  1  = OBSERVE (Inputs strukturieren)
  2  = ORIENT  (Lagebild bauen, Recherche)
  2b = DECIDE  (Stakeholder/Verarbeitungsplan)
  3c = ANALYSE (Dokumente lesen, Chunks verarbeiten)
  3  = ACT     (Stakeholder antworten, Emails senden)
  3b = VALIDATE (Antworten prüfen)
  4  = PERSIST (Workspace schreiben, Erinnerungen)

Kategorien:
  R = READ     — Informationen beschaffen (lokal)
  D = DATA     — Externe Datenquellen abfragen
  W = WRITE    — Informationen persistieren
  C = COMMUNICATE — Nachrichten an Menschen senden
  S = SCHEDULE — Zeitgesteuerte Aktionen
  E = EXTERNAL — Spezialisierte externe Systeme
"""

import logging

log = logging.getLogger("AIMOS.ToolPhaseRegistry")

# Phase codes
P0 = "0"       # KONTEXT
P1 = "1"       # OBSERVE
P2 = "2"       # ORIENT
P2b = "2b"     # DECIDE
P3c = "3c"     # ANALYSE (Dokumente)
P3 = "3"       # ACT (Stakeholder)
P3b = "3b"     # VALIDATE
P4 = "4"       # PERSIST

ALL_PHASES = {P0, P1, P2, P2b, P3c, P3, P3b, P4}
READ_PHASES = {P0, P1, P2, P2b, P3c}
ACT_PHASES = {P3}
WRITE_PHASES = {P3c, P4}  # 3c = auto-persist only
PERSIST_PHASES = {P4}

# ══════════════════════════════════════════════════════════════════════════════
#  REGISTRY: tool_name → (category, allowed_phases, description)
# ══════════════════════════════════════════════════════════════════════════════

TOOL_REGISTRY: dict[str, tuple[str, set[str], str]] = {
    # ── R: READ (local workspace) ────────────────────────────────────────
    "read_file":          ("R", {P0, P1, P2, P3c, P4},    "Workspace-Datei lesen"),
    "read_file_chunked":  ("R", {P0, P3c},                 "Große Datei seitenweise"),
    "list_workspace":     ("R", {P0, P1},                   "Dateien auflisten"),
    "read_public":        ("R", {P0, P2},                   "Öffentliche Datei (Norm, KB)"),
    "search_in_file":     ("R", {P0, P2, P3c},             "In Datei suchen"),
    "get_file_overview":  ("R", {P0, P1},                   "Datei-Übersicht"),
    "current_time":       ("R", ALL_PHASES,                  "Aktuelle Uhrzeit"),
    "system_status":      ("R", {P0},                        "System/GPU Status"),

    # ── R: READ (memory) ─────────────────────────────────────────────────
    "recall":             ("R", {P0, P2, P3},               "Langzeitgedächtnis abfragen"),
    "lookup_thread":      ("R", {P0, P2, P3},               "Thread-History nachschlagen"),
    "find_contact":       ("R", {P0, P2, P3},               "Kontakt suchen"),
    "list_contacts":      ("R", {P0, P2},                    "Alle Kontakte"),

    # ── R: READ (OCR) ───────────────────────────────────────────────────
    "ocr_extract_text":   ("R", {P3c},                      "PDF/Bild → Text"),
    "ocr_extract_fields": ("R", {P3c},                      "Strukturierte Daten aus Dokument"),
    "ocr_list_scannable": ("R", {P0, P1},                   "Scanbare Dateien auflisten"),

    # ── D: DATA (externe Datenquellen lesen) ─────────────────────────────
    "fetch_user_mail":    ("D", {P0},                        "IMAP-Postfach abrufen"),
    "search_mail":        ("D", {P0, P2},                    "Im Mail-Archiv suchen"),
    "read_mail":          ("D", {P0, P2, P3c},              "Einzelne Mail lesen"),
    "web_search":         ("D", {P2, P3c},                   "Internet-Recherche"),
    "web_browse":         ("D", {P2, P3c},                   "Webseite lesen"),

    # ── D: DATA (Dropbox) ───────────────────────────────────────────────
    "dropbox_list_folder":    ("D", {P0},                    "Dropbox-Ordner auflisten"),
    "dropbox_download_file":  ("D", {P0},                    "Datei aus Dropbox laden"),
    "dropbox_sync_folder":    ("D", {P0},                    "Ordner synchronisieren"),
    "dropbox_check_new_files":("D", {P0},                    "Neue Dateien prüfen"),
    "dropbox_get_file_info":  ("D", {P0, P2},                "Datei-Info"),

    # ── D: DATA (Codebeamer) ────────────────────────────────────────────
    "cb_search_items":        ("D", {P0, P2},                "Requirements suchen"),
    "cb_get_item":            ("D", {P2, P3c},               "Requirement lesen"),
    "cb_get_item_relations":  ("D", {P2, P3c},               "Traceability prüfen"),
    "cb_get_baselines":       ("D", {P0, P2},                "Baselines abrufen"),
    "cb_compare_baselines":   ("D", {P2},                    "Baselines vergleichen"),

    # ── D: DATA (JIRA) ──────────────────────────────────────────────────
    "jira_search_issues":     ("D", {P0, P2},                "Issues suchen"),
    "jira_get_issue":         ("D", {P2, P3c},               "Issue lesen"),

    # ── D: DATA (Azure DevOps) ──────────────────────────────────────────
    "azdo_search_work_items": ("D", {P0, P2},                "Work Items suchen"),
    "azdo_get_work_item":     ("D", {P2, P3c},               "Work Item lesen"),
    "azdo_list_pipelines":    ("D", {P0, P2},                "Pipelines auflisten"),

    # ── D: DATA (ERP/Buchhaltung) ───────────────────────────────────────
    "get_customer_balance":   ("D", {P2},                    "Kundensaldo"),
    "list_unpaid_invoices":   ("D", {P2},                    "Offene Rechnungen"),
    "search_transactions":    ("D", {P2, P3c},               "Buchungen suchen"),
    "get_daily_summary":      ("D", {P2},                    "Tageszusammenfassung"),

    # ── D: DATA (Confluence) ────────────────────────────────────────────
    "confluence_search":      ("D", {P0, P2},                "Confluence durchsuchen"),
    "confluence_get_page":    ("D", {P2, P3c},               "Seite lesen"),
    "confluence_get_space_pages": ("D", {P0},                "Space-Seiten auflisten"),

    # ── D: DATA (SharePoint) ───────────────────────────────────────────
    "sp_search_documents":    ("D", {P0, P2},                "SharePoint durchsuchen"),
    "sp_get_document":        ("D", {P2, P3c},               "Dokument lesen"),
    "sp_list_folder":         ("D", {P0},                    "Ordner auflisten"),
    "sp_get_document_content":("D", {P3c},                   "Dokumentinhalt lesen"),

    # ── D: DATA (Calendar) ──────────────────────────────────────────────
    "list_events":            ("D", {P0, P2},                "Kalendereinträge"),
    "check_today":            ("D", {P0},                    "Heutige Termine"),
    "check_overdue":          ("D", {P0},                    "Überfällige Aufgaben"),

    # ── D: DATA (Vision — externe API) ──────────────────────────────────
    "analyze_image":          ("D", {P3c},                   "Bild via externe Vision-API"),

    # ── W: WRITE (Workspace) ────────────────────────────────────────────
    "write_file":             ("W", {P4},                    "Workspace-Datei schreiben"),
    "remember":               ("W", {P4},                    "Fakt ins Langzeitgedächtnis"),
    "forget":                 ("W", {P4},                    "Fakt löschen"),
    "update_customer":        ("W", {P4},                    "Kundendaten aktualisieren"),
    "add_contact":            ("W", {P4},                    "Kontakt anlegen"),
    "store_chunk_summary":    ("W", {P3c, P4},               "Chunk-Zusammenfassung speichern"),

    # ── W: WRITE (externe Systeme) ──────────────────────────────────────
    "cb_create_item":         ("W", {P3, P4},                "Requirement anlegen"),
    "cb_update_item":         ("W", {P3, P4},                "Requirement aktualisieren"),
    "cb_add_comment":         ("W", {P3, P4},                "Kommentar zu Requirement"),
    "jira_create_issue":      ("W", {P3, P4},                "JIRA Issue anlegen"),
    "jira_update_status":     ("W", {P3, P4},                "JIRA Status ändern"),
    "jira_add_comment":       ("W", {P3, P4},                "JIRA Kommentar"),
    "azdo_create_work_item":  ("W", {P3, P4},                "Azure DevOps Work Item"),
    "azdo_update_work_item":  ("W", {P3, P4},                "Work Item aktualisieren"),
    "azdo_add_comment":       ("W", {P3, P4},                "Kommentar hinzufügen"),
    "confluence_create_page":  ("W", {P4},                   "Confluence-Seite erstellen"),
    "confluence_update_page":  ("W", {P4},                   "Confluence-Seite aktualisieren"),
    "sp_upload_document":     ("W", {P4},                    "SharePoint-Upload"),

    # ── C: COMMUNICATE (Nachrichten an Menschen) ────────────────────────
    "send_email":             ("C", {P3},                    "Email an Stakeholder"),
    "send_telegram_message":  ("C", {P3},                    "Telegram-Nachricht"),
    "send_telegram_file":     ("C", {P3},                    "Telegram-Datei senden"),
    "send_voice_message":     ("C", {P3},                    "Sprachnachricht senden"),
    "send_to_agent":          ("C", {P3},                    "An anderen Agent delegieren"),
    "teams_send_message":     ("C", {P3},                    "Teams-Nachricht"),
    "teams_create_meeting":   ("C", {P3},                    "Teams-Meeting erstellen"),
    "teams_get_messages":     ("D", {P0, P2},                "Teams-Nachrichten lesen"),
    "teams_list_channels":    ("D", {P0},                    "Teams-Kanäle auflisten"),
    "teams_list_teams":       ("D", {P0},                    "Teams auflisten"),

    # ── S: SCHEDULE (zeitgesteuert) ─────────────────────────────────────
    "set_reminder":           ("S", {P4},                    "Erinnerung setzen"),
    "add_event":              ("S", {P4},                    "Kalendereintrag erstellen"),
    "complete_event":         ("S", {P4},                    "Termin als erledigt markieren"),
    "delete_event":           ("S", {P4},                    "Termin löschen"),
    "list_jobs":              ("S", {P0, P4},                "Geplante Jobs anzeigen"),
    "add_task":               ("S", {P4},                    "Aufgabe erstellen"),
    "update_task":            ("S", {P4},                    "Aufgabe aktualisieren"),
    "complete_task":          ("S", {P4},                    "Aufgabe abschließen"),

    # ── E: EXTERNAL (spezialisierte Systeme) ─────────────────────────────
    "elster_build_declaration": ("E", {P4},                  "ELSTER-Erklärung bauen"),
    "elster_validate":          ("E", {P4},                  "ELSTER validieren"),
    "elster_submit":            ("E", {P3},                  "ELSTER einreichen (NUR nach OK!)"),
    "elster_get_status":        ("E", {P0, P2},              "ELSTER-Status abfragen"),
    "elster_get_form_fields":   ("E", {P2},                  "Formularfelder abfragen"),
    "ask_external":             ("E", {P2},                  "Externe LLM-API (Notfall)"),

    # ── Office-Dokumente (generieren) ───────────────────────────────────
    "create_word_document":     ("W", {P4},                  "Word-Dokument erstellen"),
    "create_excel_sheet":       ("W", {P4},                  "Excel erstellen"),
    "create_pptx_presentation": ("W", {P4},                  "PowerPoint erstellen"),
    "create_pdf":               ("W", {P4},                  "PDF erstellen"),
    "convert_document":         ("W", {P4},                  "Dokument konvertieren"),

    # ── Reporting ───────────────────────────────────────────────────────
    "html_report_create":       ("W", {P4},                  "HTML-Report erstellen"),
    "html_report_status_dashboard": ("W", {P4},              "Status-Dashboard"),
    "html_report_with_chart":   ("W", {P4},                  "Report mit Charts"),
    "report_daily_summary":     ("R", {P2, P4},              "Tagesübersicht"),
    "report_weekly_overview":   ("R", {P2},                   "Wochenübersicht"),
}


# ══════════════════════════════════════════════════════════════════════════════
#  Compliance Check Functions
# ══════════════════════════════════════════════════════════════════════════════

def get_allowed_phases(tool_name: str) -> set[str] | None:
    """Returns the allowed phases for a tool, or None if not registered."""
    entry = TOOL_REGISTRY.get(tool_name)
    return entry[1] if entry else None


def get_category(tool_name: str) -> str | None:
    """Returns the category code (R/D/W/C/S/E) for a tool."""
    entry = TOOL_REGISTRY.get(tool_name)
    return entry[0] if entry else None


def is_allowed_in_phase(tool_name: str, phase: str) -> bool:
    """Check if a tool is allowed in a specific OODA phase."""
    phases = get_allowed_phases(tool_name)
    if phases is None:
        log.warning(f"Tool '{tool_name}' not registered in TOOL_PHASE_REGISTRY")
        return True  # Unregistered tools are allowed (with warning)
    return phase in phases


def check_agent_compliance(config: dict) -> list[str]:
    """Check if an agent's skill configuration is OODA-compliant.

    Returns list of warnings/errors. Empty list = compliant.
    """
    warnings = []
    strategy = config.get("execution_strategy", "reactive")

    if strategy != "batch":
        return []  # Only OODA agents are checked

    skills = set(config.get("skills", []))

    # Pflicht: file_ops
    if "file_ops" not in skills:
        warnings.append("CRITICAL: Worker ohne file_ops — kann keine Workspace-Dateien lesen")

    # Pflicht: persistence
    if "persistence" not in skills:
        warnings.append("WARNING: Worker ohne persistence — kein Langzeitgedächtnis")

    # Kommunikation
    comm_skills = {"email", "ms_teams"} & skills
    if not comm_skills:
        warnings.append("WARNING: Worker ohne email/teams — kann Stakeholder nicht kontaktieren")

    # OCR bei workspace_scan
    if config.get("batch_workspace_scan") and "document_ocr" not in skills:
        warnings.append("CRITICAL: batch_workspace_scan=true ohne document_ocr")

    return warnings
