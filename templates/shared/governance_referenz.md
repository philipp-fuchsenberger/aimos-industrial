# Governance-Referenz — Wann wird Arbeit zum Projekt?

Referenz für alle AIMOS-Agenten. Lade diese Datei per read_file() wenn du
den Verdacht hast, dass ein Vorgang formalisiert werden muss.

---

## Wann ist etwas ein Projekt?

Ein Vorgang wird zum Projekt wenn MINDESTENS 2 dieser Kriterien zutreffen:
1. **Mehrere Stakeholder** — mehr als 1 Person ist beteiligt oder betroffen
2. **Deliverables** — es werden konkrete Ergebnisse erwartet (Dokument, Produkt, Entscheidung)
3. **Deadline** — es gibt einen Termin oder eine Frist
4. **Abhängigkeiten** — Ergebnisse eines Schritts sind Voraussetzung für den nächsten
5. **Budget/Aufwand** — der Vorgang bindet signifikante Ressourcen (>1 Arbeitstag)

## Was ein Projekt formal braucht

- **Projektname** — eindeutige Bezeichnung
- **Scope** — was gehört dazu, was nicht (Abgrenzung!)
- **Verantwortlicher** — wer entscheidet, wer trägt Verantwortung
- **Stakeholder-Liste** — wer ist beteiligt, wer muss informiert werden
- **Zeitplan** — Meilensteine, Deadlines
- **Akzeptanzkriterien** — wann ist das Projekt erfolgreich abgeschlossen
- **Ablagestruktur** — wo liegen die Projektdateien (Workspace-Ordner, JIRA-Projekt etc.)

## Warnsignale: Mission Creep

Mission Creep = Der Umfang einer Aufgabe wächst schleichend über das Original hinaus.

Warnsignale:
- Neue Anforderungen ohne formalen Change Request
- "Können Sie auch noch..." Emails die den Scope erweitern
- Die Anzahl beteiligter Personen wächst
- Ursprüngliche Deadline ist nicht mehr haltbar
- Aufgabe berührt Themen die nicht im ursprünglichen Auftrag standen
- Mandant/Kunde liefert ständig Nachträge

## Was der Agent tun soll

1. **Erkennen:** In Phase 3 DECIDE prüfen ob aktuelle Arbeit Projekt-Kriterien erfüllt
2. **Flaggen:** Als HIGH-Priorität in den Stakeholder-Plan aufnehmen
3. **Vorschlagen:** Dem Verantwortlichen empfehlen:
   - "Dieser Vorgang hat Projektcharakter — soll ich einen Projektplan erstellen?"
   - "Der Scope hat sich seit der letzten Sitzung erweitert — brauchen wir einen Change Request?"
4. **Dokumentieren:** In state.md/todo.md vermerken dass Formalisierung aussteht

## Branchen-Beispiele

### Steuerberater
- Einfacher Fall (kein Projekt): 1 Mandant, Standard-ESt, keine Belege → direkt einreichen
- Projektcharakter: Mandant hat 3 Immobilien, Krypto-Trading, Ehegattensplitting-Optimierung,
  Einspruch gegen Vorbescheid → braucht Fallakte, Zeitplan, Zwischenberichte

### FuSa Manager
- Routine (kein Projekt): Standard-FMEA-Review eines bekannten Bauteils
- Projektcharakter: Neues Sicherheitskonzept für autonomes Fahren, 5 Abteilungen beteiligt,
  ISO 26262 ASIL-D → formales Projekt mit Safety Plan, V&V Plan, Meilensteinplan

### Handwerker-Disposition
- Routine: Einzelner Reparaturauftrag, 1 Tag, 1 Handwerker
- Projektcharakter: Komplettsanierung Badezimmer, 3 Gewerke, 4 Wochen,
  Materialbestellung + Koordination → Projektplan mit Gewerke-Abhängigkeiten
