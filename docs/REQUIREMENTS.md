# AIMOS v4.1.0 Requirements Specification (RTM)

## 1. Prozess- & Governance-Requirements (PRQ)
| ID | Kategorie | Anforderung | Validierung |
| :--- | :--- | :--- | :--- |
| PRQ-01 | Modularität | Keine Python-Datei darf 500 Zeilen überschreiten. | `tools/validate_requirements.py` |
| PRQ-02 | Dokumentation | Jeder Logik-Commit erfordert einen Eintrag in `CHANGELOG.md`. | Git-Hook / Manual Check |
| PRQ-03 | Architektur | `ARCHITECTURE.md` muss mit der realen Ordnerstruktur übereinstimmen. | `tree -L 2` Abgleich |
| PRQ-04 | Clean Code | Keine hartkodierten Pfade; Nutzung von `core/config.py`. | Code-Review |
| PRQ-05 | Artifacts | Keine Binaries (venv, models, dumps) im Repository. | `.gitignore` Audit |

## 2. System- & Hardware-Requirements (SRQ)
| ID | Kategorie | Anforderung | Validierung |
| :--- | :--- | :--- | :--- |
| SRQ-01 | VRAM-Hygiene | VRAM-Load muss nach Task-Ende auf < 1GB sinken (RTX 3090). | Dashboard-Telemetrie |
| SRQ-02 | Isolation | Jeder Agent läuft in einem isolierten OS-Subprozess. | PID-Tracking |
| SRQ-03 | Database | Daten-Isolation durch Schema pro Agent (`memory_{id}`). | SQL Schema-Check |
| SRQ-04 | Mutex | Orchestrator verhindert parallele LLM-Loads im VRAM. | Mutex-Lock Test |

## 3. Security- & Interface-Requirements (IRQ)
| ID | Kategorie | Anforderung | Validierung |
| :--- | :--- | :--- | :--- |
| IRQ-01 | Anti-Leak | Secrets dürfen niemals in STDOUT oder Logs erscheinen. | SecretLogFilter Test |
| IRQ-02 | Lockdown | Bei `Orchestrator=ON` sind manuelle Schreibzugriffe gesperrt. | API 409 Conflict Test |
| IRQ-03 | Audit | Jede Statusänderung (Start/Stop/Crash) wird persistiert. | Tabelle `agent_logs` |
