"""
AIMOS Tool-Phase Registry — OODA Compliance
=============================================

Definiert welche Tools in welcher OODA-Phase dem LLM zur Verfügung stehen.
Nicht registrierte Tools werden mit WARNING geloggt.

OODA-Phasen (6 Phasen):
  0 = KONTEXT   — Workspace laden, Datenquellen synchronisieren
  1 = OBSERVE   — Inputs strukturieren, Inventory erstellen
  2 = ORIENT    — Lagebild bauen: Recherche + Dokument-Analyse (Loop!)
                   2a: Chunk-Loop (N LLM-Calls, je 1 pro Dokument-Chunk)
                   2b: Lagebild konsolidieren (1 LLM-Call)
                   ⚠ Sicherung: max_chunks_per_cycle + Hard-Timeout
  3 = DECIDE    — Stakeholder identifizieren, Verarbeitungsplan
  4 = ACT       — Outbound Dispatch (kann fehlschlagen, kann übersprungen werden)
                   4a: Draft generieren (LLM, KEINE Send-Tools)
                   4b: Validate (LLM, prüft Draft)
                   4c: Dispatch (ORCHESTRATOR, kein LLM)
                   Alles was nach AUSSEN geht: Email, Teams, JIRA, CB, ELSTER...
  5 = PERSIST   — Guaranteed Self-Dispatch (MUSS IMMER laufen, auch bei ACT-Crash)
                   Konzeptionell "Dispatch an sich selbst".
                   Schützt vor Amnesie. Läuft im finally-Block.
                   Config: batch_persist=false → überspringen (stateless Agent)

Architektur-Prinzip:
  Das LLM DENKT und SCHREIBT Drafts + generiert Dokumente.
  Der Orchestrator DISPATCHT nach außen (Email, Teams, JIRA, ELSTER...).
  PERSIST ist "Self-Dispatch" — gleiche Mechanik, aber guaranteed.

  Dispatch-Ziele:
    Outbound (Phase 4): Email, Teams, Telegram, JIRA, CB, AzDO, ELSTER, SharePoint
    Self (Phase 5):     Workspace (state.md), Gedächtnis (remember), Termine (set_reminder)

Kategorien (nur LLM-seitige Tools):
  R = READ     — Informationen beschaffen (lokal + Gedächtnis)
  D = DATA     — Externe Datenquellen abfragen (nur lesen!)
  W = WRITE    — Workspace-Dateien schreiben, Gedächtnis aktualisieren
  S = SCHEDULE — Erinnerungen, Termine, Aufgaben erstellen
  E = EXTERNAL — Spezialisierte externe Systeme (ELSTER, Vision-API)

Nicht mehr als LLM-Tool:
  C = COMMUNICATE — wird vom Orchestrator ausgeführt, nicht vom LLM
"""

import logging
from typing import Optional

log = logging.getLogger("AIMOS.ToolPhaseRegistry")

# ── Phase Constants ───────────────────────────────────────────────────────────

P_KONTEXT  = "0"
P_OBSERVE  = "1"
P_ORIENT   = "2"   # Includes 2a (chunk-loop) + 2b (consolidation)
P_DECIDE   = "3"
P_ACT      = "4"   # Includes 4a (draft) + 4b (validate) + 4c (dispatch by orchestrator)
P_PERSIST  = "5"     # Guaranteed self-dispatch — ALWAYS runs, even if ACT fails

ALL_PHASES = {P_KONTEXT, P_OBSERVE, P_ORIENT, P_DECIDE, P_ACT, P_PERSIST}

PHASE_NAMES = {
    P_KONTEXT:  "KONTEXT",
    P_OBSERVE:  "OBSERVE",
    P_ORIENT:   "ORIENT",
    P_DECIDE:   "DECIDE",
    P_ACT:      "ACT",
    P_PERSIST:  "PERSIST",
}

# Sub-phases (for tool filtering within a phase)
P_ORIENT_CHUNK = "2a"   # Chunk-Loop innerhalb ORIENT
P_ORIENT_CONSOL = "2b"  # Lagebild-Konsolidierung
P_ACT_DRAFT = "4a"      # Draft generieren
P_ACT_VALIDATE = "4b"   # Draft prüfen

# ══════════════════════════════════════════════════════════════════════════════
#  REGISTRY: tool_name → (category, allowed_phases, description)
# ══════════════════════════════════════════════════════════════════════════════

TOOL_REGISTRY: dict[str, tuple[str, set[str], str]] = {

    # ══════════════════════════════════════════════════════════════════════
    # R: READ — Informationen beschaffen
    # Begründung: Lesen ist Voraussetzung für Analyse. Nur in Gather-Phasen,
    # plus recall/contacts in ACT (Agent braucht Wissen beim Antwort-Drafting).
    # ══════════════════════════════════════════════════════════════════════

    "read_file":          ("R", {P_KONTEXT, P_OBSERVE, P_ORIENT, P_ACT, P_PERSIST},
                           "Workspace-Datei lesen"),
    "read_file_chunked":  ("R", {P_KONTEXT, P_ORIENT},
                           "Große Datei seitenweise — Kontext + Analyse"),
    "list_workspace":     ("R", {P_KONTEXT, P_OBSERVE},
                           "Dateien auflisten — Inventar"),
    "read_public":        ("R", {P_KONTEXT, P_ORIENT},
                           "Öffentliche Datei (Norm, KB) — Lagebild"),
    "search_in_file":     ("R", {P_KONTEXT, P_ORIENT},
                           "In Datei suchen — Recherche"),
    "get_file_overview":  ("R", {P_KONTEXT, P_OBSERVE},
                           "Datei-Übersicht — Inventar"),
    "current_time":       ("R", ALL_PHASES,
                           "Uhrzeit — immer verfügbar"),
    "system_status":      ("R", {P_KONTEXT},
                           "GPU/System — nur beim Start"),
    "recall":             ("R", {P_KONTEXT, P_ORIENT, P_ACT},
                           "Langzeitgedächtnis — Kontext, Lagebild, Draft"),
    "lookup_thread":      ("R", {P_KONTEXT, P_ORIENT, P_ACT},
                           "Thread-History — für kontextbezogene Drafts"),
    "find_contact":       ("R", {P_KONTEXT, P_ORIENT, P_ACT},
                           "Kontakt suchen — für Adressierung in Drafts"),
    "list_contacts":      ("R", {P_KONTEXT, P_ORIENT},
                           "Alle Kontakte — Übersicht"),
    "ocr_extract_text":   ("R", {P_ORIENT},
                           "PDF/Bild → Text — Dokument-Analyse in ORIENT"),
    "ocr_extract_fields": ("R", {P_ORIENT},
                           "Strukturierte Daten — Dokument-Analyse"),
    "ocr_list_scannable": ("R", {P_KONTEXT, P_OBSERVE},
                           "Scanbare Dateien — Inventar"),
    "report_daily_summary":  ("R", {P_ORIENT, P_PERSIST},
                              "Tagesübersicht lesen"),
    "report_weekly_overview": ("R", {P_ORIENT},
                               "Wochenübersicht lesen"),

    # ══════════════════════════════════════════════════════════════════════
    # D: DATA — Externe Datenquellen (NUR LESEN)
    # Begründung: Externe Quellen nur in KONTEXT (sync) und ORIENT (Recherche).
    # In ACT wird nicht mehr recherchiert — das Lagebild ist fertig.
    # ══════════════════════════════════════════════════════════════════════

    # Email
    "fetch_user_mail":    ("D", {P_KONTEXT},               "IMAP sync — nur beim Start"),
    "search_mail":        ("D", {P_KONTEXT, P_ORIENT},     "Mail-Archiv — Kontext + Lagebild"),
    "read_mail":          ("D", {P_KONTEXT, P_ORIENT},     "Mail lesen — Kontext + Analyse"),
    # Web
    "web_search":         ("D", {P_ORIENT},                "Internet-Recherche — nur Lagebild"),
    "web_browse":         ("D", {P_ORIENT},                "Webseite lesen — nur Lagebild"),
    # Dropbox
    "dropbox_list_folder":    ("D", {P_KONTEXT},           "Dropbox auflisten"),
    "dropbox_download_file":  ("D", {P_KONTEXT},           "Datei laden"),
    "dropbox_sync_folder":    ("D", {P_KONTEXT},           "Ordner sync"),
    "dropbox_check_new_files":("D", {P_KONTEXT},           "Neue Dateien"),
    "dropbox_get_file_info":  ("D", {P_KONTEXT, P_ORIENT}, "Datei-Info"),
    # Codebeamer
    "cb_search_items":        ("D", {P_KONTEXT, P_ORIENT}, "Requirements suchen"),
    "cb_get_item":            ("D", {P_ORIENT},            "Requirement lesen"),
    "cb_get_item_relations":  ("D", {P_ORIENT},            "Traceability prüfen"),
    "cb_get_baselines":       ("D", {P_KONTEXT, P_ORIENT}, "Baselines"),
    "cb_compare_baselines":   ("D", {P_ORIENT},            "Baselines vergleichen"),
    # JIRA
    "jira_search_issues":     ("D", {P_KONTEXT, P_ORIENT}, "Issues suchen"),
    "jira_get_issue":         ("D", {P_ORIENT},            "Issue lesen"),
    # Azure DevOps
    "azdo_search_work_items": ("D", {P_KONTEXT, P_ORIENT}, "Work Items suchen"),
    "azdo_get_work_item":     ("D", {P_ORIENT},            "Work Item lesen"),
    "azdo_list_pipelines":    ("D", {P_KONTEXT, P_ORIENT}, "Pipelines"),
    # ERP
    "get_customer_balance":   ("D", {P_ORIENT},            "Kundensaldo"),
    "list_unpaid_invoices":   ("D", {P_ORIENT},            "Offene Rechnungen"),
    "search_transactions":    ("D", {P_ORIENT},            "Buchungen suchen"),
    "get_daily_summary":      ("D", {P_ORIENT},            "Tageszusammenfassung"),
    # Confluence
    "confluence_search":          ("D", {P_KONTEXT, P_ORIENT}, "Confluence suchen"),
    "confluence_get_page":        ("D", {P_ORIENT},            "Seite lesen"),
    "confluence_get_space_pages": ("D", {P_KONTEXT},           "Space-Seiten"),
    # SharePoint
    "sp_search_documents":    ("D", {P_KONTEXT, P_ORIENT}, "SharePoint suchen"),
    "sp_get_document":        ("D", {P_ORIENT},            "Dokument-Metadaten"),
    "sp_get_document_content":("D", {P_ORIENT},            "Dokumentinhalt"),
    "sp_list_folder":         ("D", {P_KONTEXT},           "Ordner auflisten"),
    "sp_list_sites":          ("D", {P_KONTEXT},           "Sites"),
    # Calendar
    "list_events":            ("D", {P_KONTEXT, P_ORIENT}, "Kalender"),
    "check_today":            ("D", {P_KONTEXT},           "Heutige Termine"),
    "check_overdue":          ("D", {P_KONTEXT},           "Überfällige"),
    # Teams (LESEN)
    "teams_get_messages":     ("D", {P_KONTEXT, P_ORIENT}, "Teams lesen"),
    "teams_list_channels":    ("D", {P_KONTEXT},           "Kanäle"),
    "teams_list_teams":       ("D", {P_KONTEXT},           "Teams"),
    # Vision
    "analyze_image":          ("D", {P_ORIENT},
                               "Bild via externe Vision-API — wenn OCR versagt"),

    # ══════════════════════════════════════════════════════════════════════
    # W: WRITE — Workspace + externe Systeme SCHREIBEN
    # Begründung: Schreiben NUR in PERSIST (Phase 5).
    # Ausnahme: store_chunk_summary in ORIENT (auto-persist der Chunk-Analyse).
    # ══════════════════════════════════════════════════════════════════════

    # Workspace — write_file auch in ACT erlaubt für Dokument-Generierung
    # (z.B. Steuerberater erstellt Aufstellung als PDF-Anhang für Email)
    "write_file":             ("W", {P_ACT, P_PERSIST},    "Workspace-Datei schreiben"),
    "remember":               ("W", {P_PERSIST},           "Langzeitgedächtnis"),
    "forget":                 ("W", {P_PERSIST},           "Fakt löschen"),
    "update_customer":        ("W", {P_PERSIST},           "Kundendaten"),
    "add_contact":            ("W", {P_PERSIST},           "Kontakt anlegen"),
    "store_chunk_summary":    ("W", {P_ORIENT, P_PERSIST}, "Chunk-Ergebnis (auto-persist)"),
    # Externe Systeme schreiben — ORCHESTRATOR DISPATCH (nicht im LLM-Tool-Set!)
    # cb_create_item, jira_create_issue, azdo_create_work_item etc.
    # sind in ORCHESTRATOR_DISPATCH_TOOLS → LLM drafted Inhalt, Orchestrator schreibt
    # Office-Dokumente — auch in ACT (Agent erstellt Aufstellung/Report als Email-Anhang)
    "create_word_document":     ("W", {P_ACT, P_PERSIST},  "Word — für Mandant/Stakeholder"),
    "create_excel_sheet":       ("W", {P_ACT, P_PERSIST},  "Excel — Aufstellung/Übersicht"),
    "create_pptx_presentation": ("W", {P_ACT, P_PERSIST},  "PowerPoint"),
    "create_pdf":               ("W", {P_ACT, P_PERSIST},  "PDF — Zusammenfassung/Report"),
    "convert_document":         ("W", {P_ACT, P_PERSIST},  "Konvertieren"),
    # Reports
    "html_report_create":           ("W", {P_ACT, P_PERSIST},  "HTML-Report — als Email-Anhang"),
    "html_report_status_dashboard": ("W", {P_ACT, P_PERSIST},  "Dashboard"),
    "html_report_with_chart":       ("W", {P_ACT, P_PERSIST},  "Report + Charts"),

    # ══════════════════════════════════════════════════════════════════════
    # S: SCHEDULE — Zeitgesteuerte Aktionen
    # Begründung: Nur in PERSIST — man plant keine Termine mitten in der Analyse.
    # ══════════════════════════════════════════════════════════════════════

    "set_reminder":           ("S", {P_PERSIST},           "Erinnerung"),
    "add_event":              ("S", {P_PERSIST},           "Termin"),
    "complete_event":         ("S", {P_PERSIST},           "Termin erledigt"),
    "delete_event":           ("S", {P_PERSIST},           "Termin löschen"),
    "list_jobs":              ("S", {P_KONTEXT, P_PERSIST},"Geplante Jobs"),
    "add_task":               ("S", {P_PERSIST},           "Aufgabe"),
    "update_task":            ("S", {P_PERSIST},           "Aufgabe aktualisieren"),
    "complete_task":          ("S", {P_PERSIST},           "Aufgabe abschließen"),

    # ══════════════════════════════════════════════════════════════════════
    # E: EXTERNAL — Spezialisierte Systeme
    # Begründung: Phase abhängig vom Zweck.
    # ══════════════════════════════════════════════════════════════════════

    "elster_build_declaration": ("E", {P_ACT, P_PERSIST},   "ELSTER bauen — Dokument für Dispatch"),
    "elster_validate":          ("E", {P_ACT, P_PERSIST},  "ELSTER validieren — vor Dispatch"),
    # elster_submit → ORCHESTRATOR_DISPATCH_TOOLS (nur nach Mandant-OK)
    "elster_get_status":        ("E", {P_KONTEXT, P_ORIENT}, "ELSTER Status"),
    "elster_get_form_fields":   ("E", {P_ORIENT},          "ELSTER Felder"),
    "ask_external":             ("E", {P_ORIENT},          "Externe LLM-API (Notfall)"),
}

# ══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR-DISPATCH-Tools — NICHT im LLM-Tool-Set!
# Der Orchestrator ruft diese nach Phase 4b (Validate) direkt auf.
# Das LLM sieht sie nie — es generiert Drafts, der Orchestrator dispatcht.
#
# Kategorie C: COMMUNICATE — Nachrichten an Menschen/Agenten
# Kategorie X: EXTERNAL WRITE — Schreibzugriff auf externe Systeme
#
# Alles was nach AUSSEN geht (nicht in den eigenen Workspace) ist Dispatch.
# ══════════════════════════════════════════════════════════════════════════════

ORCHESTRATOR_DISPATCH_TOOLS = {
    # C: COMMUNICATE — Nachrichten an Menschen
    "send_email",
    "send_telegram_message",
    "send_telegram_file",
    "send_voice_message",
    "send_to_agent",
    "teams_send_message",
    "teams_create_meeting",

    # X: EXTERNAL WRITE — Schreiben in externe Systeme
    # (LLM drafted den Inhalt, Orchestrator führt den Write aus)
    "cb_create_item",
    "cb_update_item",
    "cb_add_comment",
    "jira_create_issue",
    "jira_update_status",
    "jira_add_comment",
    "azdo_create_work_item",
    "azdo_update_work_item",
    "azdo_add_comment",
    "confluence_create_page",
    "confluence_update_page",
    "sp_upload_document",
    "elster_submit",
}


# ══════════════════════════════════════════════════════════════════════════════
#  API Functions
# ══════════════════════════════════════════════════════════════════════════════

def get_allowed_phases(tool_name: str) -> Optional[set[str]]:
    """Returns the allowed phases for a tool, or None if not registered."""
    if tool_name in ORCHESTRATOR_DISPATCH_TOOLS:
        return set()  # Never available to LLM
    entry = TOOL_REGISTRY.get(tool_name)
    return entry[1] if entry else None


def get_category(tool_name: str) -> Optional[str]:
    """Returns the category code (R/D/W/S/E) for a tool, or 'C' for dispatch tools."""
    if tool_name in ORCHESTRATOR_DISPATCH_TOOLS:
        return "C"
    entry = TOOL_REGISTRY.get(tool_name)
    return entry[0] if entry else None


def is_allowed_in_phase(tool_name: str, phase: str) -> bool:
    """Check if a tool is allowed in a specific OODA phase."""
    if tool_name in ORCHESTRATOR_DISPATCH_TOOLS:
        return False  # Never allowed for LLM
    phases = get_allowed_phases(tool_name)
    if phases is None:
        log.warning(f"Tool '{tool_name}' not registered in TOOL_PHASE_REGISTRY")
        return True  # Unregistered tools allowed with warning
    return phase in phases


def get_tools_for_phase(phase: str) -> list[str]:
    """Returns all tool names the LLM may use in a specific phase."""
    return [name for name, (_, phases, _) in TOOL_REGISTRY.items() if phase in phases]


def filter_tools_for_phase(all_tools: list[dict], phase: str) -> list[dict]:
    """Filter Ollama tool definitions to only those allowed in a phase.

    Removes COMMUNICATE tools entirely (orchestrator handles those).
    Removes tools not allowed in the current phase.

    Args:
        all_tools: List of tool dicts (Ollama format with 'function.name')
        phase: Current OODA phase ("0"-"5")

    Returns:
        Filtered list containing only tools allowed in this phase.
    """
    allowed = set(get_tools_for_phase(phase))
    filtered = []
    for tool in all_tools:
        name = tool.get("function", {}).get("name", "")
        if name in ORCHESTRATOR_DISPATCH_TOOLS:
            continue  # Never expose to LLM
        if name in allowed or name not in TOOL_REGISTRY:
            filtered.append(tool)
    if len(filtered) < len(all_tools):
        log.debug(
            f"Phase {phase} ({PHASE_NAMES.get(phase, '?')}): "
            f"{len(filtered)}/{len(all_tools)} tools for LLM "
            f"({len(all_tools) - len(filtered)} filtered out)"
        )
    return filtered


def check_agent_compliance(config: dict) -> list[str]:
    """Check if an agent's configuration is OODA-compliant."""
    warnings = []
    strategy = config.get("execution_strategy", "reactive")

    if strategy != "batch":
        return []

    skills = set(config.get("skills", []))

    if "file_ops" not in skills:
        warnings.append("CRITICAL: Worker ohne file_ops")
    if "persistence" not in skills:
        warnings.append("WARNING: Worker ohne persistence")
    if config.get("batch_workspace_scan") and "document_ocr" not in skills:
        warnings.append("CRITICAL: batch_workspace_scan=true ohne document_ocr")

    return warnings
