"""
FileOpsSkill – Universal Document & Office Toolbox fuer AIMOS-Agenten.

Wrapper um System-Tools (LibreOffice, Pandoc, Poppler, Tesseract, zip/unzip).
Agenten koennen diese Operationen ueber enrich_context() oder direkte Aufrufe nutzen.

Voraussetzungen (System-Pakete):
  - libreoffice-nogui   (DOCX/ODT/PDF Konvertierung)
  - pandoc              (Markdown/HTML/DOCX Konvertierung)
  - poppler-utils       (pdftotext, pdfinfo)
  - tesseract-ocr       (OCR fuer gescannte PDFs)
  - zip, unzip, p7zip-full

Aktivierung:
  Skill 'file_ops' in der Agent-Konfiguration listen.

Workspace:
  Alle Operationen arbeiten relativ zum AIMOS_WORKSPACE des Agenten
  (storage/agents/{agent_name}/).
"""

import logging
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Optional

from .base import BaseSkill

# Big-File-Strategy (CR-091): threshold and chunk size in estimated tokens
_BIG_FILE_THRESHOLD_TOKENS = 3_000   # files above this → chunking mode (Qwen 3.5:27b has 14K context)
_CHUNK_SIZE_TOKENS = 2_000           # ~2k tokens per chunk — leaves room for system prompt + tools
_CHARS_PER_TOKEN = 4                 # conservative estimate

logger = logging.getLogger("FileOpsSkill")


class FileOpsSkill(BaseSkill):
    """Document & Office Toolbox — Python-Wrapper fuer System-CLI-Tools."""

    name = "file_ops"
    display_name = "Office (PDF/Word/ZIP)"

    def __init__(self, agent_name: str = "") -> None:
        self._agent_name = agent_name
        self._workspace = self.workspace_path(agent_name) if agent_name else Path("storage/agents/_default")

    def is_available(self) -> bool:
        """True wenn mindestens LibreOffice oder Pandoc verfuegbar ist."""
        return bool(shutil.which("libreoffice") or shutil.which("pandoc"))

    async def enrich_context(self, user_text: str) -> str:
        """Informiert den Agenten ueber verfuegbare Werkzeuge und Workspace-Inhalt."""
        tools = []
        if shutil.which("libreoffice"):
            tools.append("LibreOffice (DOCX/ODT/PDF)")
        if shutil.which("pandoc"):
            tools.append("Pandoc (Markdown/HTML/DOCX)")
        if shutil.which("pdftotext"):
            tools.append("pdftotext (PDF-Extraktion)")
        if shutil.which("tesseract"):
            tools.append("Tesseract OCR")
        if shutil.which("zip"):
            tools.append("zip/unzip")

        if not tools:
            return ""

        ws_files = self._list_workspace()
        files_info = ""
        if ws_files:
            files_info = "\nDateien im Workspace:\n" + "\n".join(f"  - {f}" for f in ws_files[:20])
            if len(ws_files) > 20:
                files_info += f"\n  ... und {len(ws_files) - 20} weitere"

        return (
            f"[File-Ops Toolbox]\n"
            f"Verfuegbare Werkzeuge: {', '.join(tools)}\n"
            f"Workspace: {self._workspace}"
            f"{files_info}\n"
        )

    # ── Tool Definitions ───────────────────────────────────────────────────

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "list_workspace",
                "description": "Zeigt alle Dateien im Workspace des Agenten.",
                "parameters": {},
            },
            {
                "name": "convert_document",
                "description": "Konvertiert ein Dokument (z.B. DOCX zu PDF, ODT zu DOCX).",
                "parameters": {
                    "input_file":    {"type": "string", "description": "Quelldatei im Workspace", "required": True},
                    "output_format": {"type": "string", "description": "Zielformat: pdf, docx, odt, html, txt", "required": True},
                },
            },
            {
                "name": "extract_pdf_text",
                "description": "Extrahiert Text aus einer PDF-Datei (mit OCR-Fallback fuer Scans).",
                "parameters": {
                    "pdf_file": {"type": "string", "description": "PDF-Datei im Workspace", "required": True},
                },
            },
            {
                "name": "read_file_chunked",
                "description": (
                    "Liest einen Chunk einer grossen Datei (>32k Tokens). "
                    "Gibt Chunk-Inhalt + Metadaten (total_chunks, chars) zurueck. "
                    "Nach jedem Chunk: Zusammenfassung mit store_chunk_summary speichern, dann naechsten Chunk lesen."
                ),
                "parameters": {
                    "filename": {"type": "string", "description": "Dateiname im Workspace", "required": True},
                    "chunk":    {"type": "integer", "description": "Chunk-Nummer (0-basiert, default=0)", "required": False},
                },
            },
            {
                "name": "store_chunk_summary",
                "description": (
                    "Speichert eine Zusammenfassung fuer einen Chunk einer grossen Datei in memory.db. "
                    "Nutze dies nach jedem read_file_chunked Aufruf."
                ),
                "parameters": {
                    "filename": {"type": "string", "description": "Dateiname", "required": True},
                    "chunk":    {"type": "integer", "description": "Chunk-Nummer", "required": True},
                    "summary":  {"type": "string", "description": "Zusammenfassung des Chunk-Inhalts", "required": True},
                },
            },
            {
                "name": "get_file_overview",
                "description": (
                    "Zeigt alle gespeicherten Chunk-Zusammenfassungen einer Datei. "
                    "Gibt einen Gesamtueberblick ueber den Inhalt einer grossen Datei."
                ),
                "parameters": {
                    "filename": {"type": "string", "description": "Dateiname", "required": True},
                },
            },
            {
                "name": "search_in_file",
                "description": (
                    "Durchsucht eine Datei nach einem Stichwort oder Regex-Pattern. "
                    "Gibt nur die relevanten Passagen mit Kontext zurueck, statt die gesamte Datei zu laden. "
                    "Ideal fuer grosse Dateien."
                ),
                "parameters": {
                    "filename": {"type": "string", "description": "Dateiname im Workspace", "required": True},
                    "query":    {"type": "string", "description": "Suchbegriff oder Regex-Pattern", "required": True},
                    "context_lines": {"type": "integer", "description": "Kontextzeilen vor/nach Treffer (default=3)", "required": False},
                },
            },
            {
                "name": "create_pdf",
                "description": (
                    "Erstellt ein formatiertes PDF-Dokument aus Text. "
                    "Ideal fuer Angebote, Berichte, Zusammenfassungen. "
                    "Gibt den Dateinamen der erstellten PDF zurueck."
                ),
                "parameters": {
                    "filename": {"type": "string", "description": "Zieldateiname (z.B. angebot_kunde.pdf)", "required": True},
                    "title":    {"type": "string", "description": "Dokumenttitel", "required": True},
                    "content":  {"type": "string", "description": "Dokumentinhalt (Markdown-aehnlich: # fuer Ueberschriften, - fuer Listen, | fuer Tabellen)", "required": True},
                },
            },
        ]

    async def execute_tool(self, tool_name: str, arguments: dict) -> str:
        logger.info(f"[FILE_OPS] execute_tool: {tool_name}({arguments})")
        if tool_name == "list_workspace":
            files = self.list_files()
            if not files:
                return "Workspace ist leer."
            lines = [f"  {f['name']:30} {f['size_bytes']:>8} bytes  ({f['suffix']})" for f in files]
            return f"Workspace ({self._workspace}):\n" + "\n".join(lines)

        elif tool_name == "convert_document":
            try:
                # Tolerant param names — LLMs use varying keys
                input_file = (arguments.get("input_file") or arguments.get("filename")
                              or arguments.get("file") or arguments.get("source", ""))
                output_format = (arguments.get("output_format") or arguments.get("format")
                                 or arguments.get("to") or arguments.get("target_format", "pdf"))
                if not input_file:
                    return "Fehler: Quelldatei fehlt (input_file)."
                result = self.convert_document(input_file, output_format)
                return f"Konvertiert: {input_file} -> {result}"
            except Exception as exc:
                return f"Konvertierung fehlgeschlagen: {exc}"

        elif tool_name == "extract_pdf_text":
            try:
                pdf_file = arguments.get("pdf_file") or arguments.get("filename") or arguments.get("file", "")
                text = self.extract_pdf_text(pdf_file)
                if not text:
                    return "Keine Textinhalte in der PDF gefunden."
                return f"PDF-Text ({len(text)} Zeichen):\n{text[:3000]}"
            except Exception as exc:
                return f"PDF-Extraktion fehlgeschlagen: {exc}"

        elif tool_name == "read_file_chunked":
            try:
                filename = arguments.get("filename") or arguments.get("file", "")
                chunk = int(arguments.get("chunk", 0))
                return self._read_file_chunked(filename, chunk)
            except Exception as exc:
                return f"Chunk-Lesen fehlgeschlagen: {exc}"

        elif tool_name == "store_chunk_summary":
            try:
                filename = arguments.get("filename", "")
                chunk = int(arguments.get("chunk", 0))
                summary = arguments.get("summary", "")
                if not filename or not summary:
                    return "Fehler: 'filename' und 'summary' sind Pflichtfelder."
                return self._store_chunk_summary(filename, chunk, summary)
            except Exception as exc:
                return f"Speichern fehlgeschlagen: {exc}"

        elif tool_name == "get_file_overview":
            try:
                filename = arguments.get("filename", "")
                if not filename:
                    return "Fehler: 'filename' ist ein Pflichtfeld."
                return self._get_file_overview(filename)
            except Exception as exc:
                return f"Uebersicht fehlgeschlagen: {exc}"

        elif tool_name == "search_in_file":
            try:
                filename = arguments.get("filename") or arguments.get("file", "")
                query = arguments.get("query") or arguments.get("search", "")
                context_lines = int(arguments.get("context_lines", 3))
                if not filename or not query:
                    return "Fehler: 'filename' und 'query' sind Pflichtfelder."
                return self._search_in_file(filename, query, context_lines)
            except Exception as exc:
                return f"Suche fehlgeschlagen: {exc}"

        elif tool_name == "create_pdf":
            filename = arguments.get("filename", "document.pdf")
            title = arguments.get("title", "Dokument")
            content = arguments.get("content", "")
            if not content:
                return "Fehler: 'content' ist leer."
            if not filename.endswith(".pdf"):
                filename += ".pdf"
            try:
                return self._create_pdf(filename, title, content)
            except Exception as exc:
                return f"PDF-Erstellung fehlgeschlagen: {exc}"

        return f"Unbekanntes Tool: {tool_name}"

    def _create_pdf(self, filename: str, title: str, content: str) -> str:
        """Create a branded PDF document with configurable corporate design.

        Uses agent config for branding. Falls back to neutral blue design.
        """
        from fpdf import FPDF
        from datetime import datetime

        # Brand colors — configurable via agent config, default neutral blue
        brand = self._config.get("pdf_branding", {}) if hasattr(self, '_config') else {}
        NAVY = tuple(brand.get("primary_color", [0, 58, 93]))
        CYAN = tuple(brand.get("accent_color", [0, 159, 227]))
        DARK_TEXT = (29, 60, 78)
        LIGHT_BG = (242, 242, 242)
        RULE_GRAY = (204, 204, 204)
        company_name = brand.get("company_name", "")
        company_footer = brand.get("company_footer", "")

        _title = title  # capture for inner class
        _font = "Helvetica"  # safe default before DejaVu try/except

        class BrandedPDF(FPDF):
            def header(self):
                # Thin navy rule at top
                self.set_draw_color(*NAVY)
                self.line(10, 10, 200, 10)
                # Left: Company name
                self.set_xy(10, 12)
                self.set_font(_font, "B", 8)
                self.set_text_color(*NAVY)
                self.cell(80, 5, company_name)
                # Right: document title + page
                self.set_xy(120, 12)
                self.set_font(_font, "", 8)
                self.set_text_color(*NAVY)
                self.cell(80, 5, _title[:40], align="R")
                self.ln(10)

            def footer(self):
                self.set_y(-12)
                self.set_draw_color(*RULE_GRAY)
                self.line(10, self.get_y(), 200, self.get_y())
                self.set_font(_font, "", 7)
                self.set_text_color(150, 150, 150)
                self.cell(0, 8,
                    f"{company_footer} | Seite {self.page_no()}/{{nb}}" if company_footer else f"Seite {self.page_no()}/{{nb}}",
                    align="C")

        pdf = BrandedPDF(orientation="P", unit="mm", format="A4")
        # DejaVu for full Unicode support (umlauts, €, etc.)
        _fd = "/usr/share/fonts/truetype/dejavu/"
        try:
            pdf.add_font("DV", "", _fd + "DejaVuSans.ttf")
            pdf.add_font("DV", "B", _fd + "DejaVuSans-Bold.ttf")
            _font = "DV"
        except Exception:
            _font = "Helvetica"  # Fallback
        pdf.alias_nb_pages()
        pdf.set_auto_page_break(auto=True, margin=20)
        pdf.add_page()

        # Title block — navy rectangle with white text + cyan accent square
        y = pdf.get_y()
        pdf.set_fill_color(*NAVY)
        pdf.rect(10, y, 170, 14, "F")
        # Cyan accent square
        pdf.set_fill_color(*CYAN)
        pdf.rect(180, y, 10, 14, "F")
        # White title text
        pdf.set_xy(14, y + 2)
        pdf.set_font(_font, "B", 14)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(160, 10, title[:60])
        pdf.ln(18)

        # Content parsing with proper table rendering
        in_table = False
        table_rows = []

        def _flush_table():
            nonlocal table_rows, in_table
            if not table_rows:
                in_table = False
                return
            parsed = []
            max_cols = 0
            for row in table_rows:
                cells = [c.strip().replace("**", "") for c in row.split("|") if c.strip()]
                parsed.append(cells)
                max_cols = max(max_cols, len(cells))
            if not parsed or max_cols == 0:
                in_table = False
                table_rows = []
                return
            col_w = 180 / max_cols
            # Header row
            pdf.set_font(_font, "B", 8)
            pdf.set_text_color(*NAVY)
            pdf.set_fill_color(*LIGHT_BG)
            pdf.set_x(10)
            for c in parsed[0]:
                pdf.cell(col_w, 5, c[:30], border=0, fill=True)
            pdf.ln()
            pdf.set_draw_color(*CYAN)
            pdf.line(10, pdf.get_y(), 200, pdf.get_y())
            pdf.ln(1)
            # Data rows
            pdf.set_font(_font, "", 8)
            pdf.set_text_color(*DARK_TEXT)
            for row_cells in parsed[1:]:
                pdf.set_x(10)
                for c in row_cells:
                    bold = c.startswith("*")
                    clean = c.replace("*", "")
                    if bold:
                        pdf.set_font(_font, "B", 8)
                    pdf.cell(col_w, 5, clean[:30], border=0)
                    if bold:
                        pdf.set_font(_font, "", 8)
                pdf.ln()
            pdf.ln(2)
            in_table = False
            table_rows = []

        # LLMs sometimes write literal \n instead of actual newlines
        content = content.replace("\\n", "\n")
        # Strip leading # heading if it duplicates the PDF title bar
        _content_lines = content.split("\n")
        if _content_lines and _content_lines[0].strip().lstrip("#").strip().lower() == title.strip().lower():
            _content_lines = _content_lines[1:]  # skip duplicate title
        for line in _content_lines:
            line = line.rstrip()
            # Table rows
            if line.startswith("| "):
                if "---" in line and all(c in "|-: " for c in line):
                    continue
                if not in_table:
                    in_table = True
                    table_rows = []
                table_rows.append(line)
                continue
            elif in_table:
                _flush_table()

            if not line:
                pdf.ln(2)
                continue
            if line.startswith("## "):
                pdf.ln(4)
                y_h = pdf.get_y()
                pdf.set_fill_color(*CYAN)
                pdf.rect(10, y_h, 2, 7, "F")
                pdf.set_x(15)
                pdf.set_font(_font, "B", 11)
                pdf.set_text_color(*NAVY)
                pdf.cell(0, 7, line[3:], new_x="LMARGIN", new_y="NEXT")
                pdf.ln(2)
            elif line.startswith("# "):
                pdf.ln(5)
                pdf.set_font(_font, "B", 13)
                pdf.set_text_color(*NAVY)
                pdf.set_x(10)
                pdf.cell(0, 8, line[2:], new_x="LMARGIN", new_y="NEXT")
                pdf.ln(3)
            elif line.startswith("---"):
                pdf.ln(2)
                pdf.set_draw_color(*RULE_GRAY)
                pdf.line(10, pdf.get_y(), 200, pdf.get_y())
                pdf.ln(3)
            elif line.startswith("- "):
                pdf.set_font(_font, "", 9)
                pdf.set_text_color(*CYAN)
                pdf.set_x(14)
                pdf.cell(4, 5, ">")
                pdf.set_text_color(*DARK_TEXT)
                pdf.set_x(18)
                pdf.multi_cell(0, 5, line[2:].replace("**", ""))
            elif line.startswith("**") and line.endswith("**"):
                pdf.set_font(_font, "B", 10)
                pdf.set_text_color(*DARK_TEXT)
                pdf.set_x(10)
                pdf.multi_cell(0, 5, line.replace("**", ""))
            else:
                pdf.set_font(_font, "", 9)
                pdf.set_text_color(*DARK_TEXT)
                pdf.set_x(10)
                pdf.multi_cell(0, 5, line.replace("**", ""))

        if in_table:
            _flush_table()

        # Final footer note
        pdf.ln(8)
        pdf.set_draw_color(*NAVY)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.ln(3)
        pdf.set_font(_font, "", 7)
        pdf.set_text_color(150, 150, 150)
        pdf.multi_cell(0, 3.5,
            "Dieses Angebot wurde automatisch erstellt. Preise freibleibend, "
            "Irrtum und Zwischenverfuegbarkeit vorbehalten. "
            "Es gelten unsere allgemeinen Geschaeftsbedingungen.")

        out_path = self._workspace / filename
        pdf.output(str(out_path))
        size = out_path.stat().st_size
        logger.info(f"[FILE_OPS] PDF created: {out_path} ({size} bytes)")
        return f"PDF erstellt: {filename} ({size} bytes)"

    # ── Public API fuer Agenten und chat.py ──────────────────────────────────

    def _list_workspace(self) -> list[str]:
        """Listet alle Dateien im Workspace."""
        if not self._workspace.exists():
            return []
        return sorted(f.name for f in self._workspace.iterdir() if f.is_file())

    def list_files(self) -> list[dict]:
        """Gibt Workspace-Dateien mit Metadaten zurueck."""
        if not self._workspace.exists():
            return []
        result = []
        for f in sorted(self._workspace.iterdir()):
            if f.is_file():
                result.append({
                    "name": f.name,
                    "size_bytes": f.stat().st_size,
                    "suffix": f.suffix.lower(),
                })
        return result

    def convert_document(
        self,
        input_file: str,
        output_format: str,
        output_file: Optional[str] = None,
    ) -> str:
        """Konvertiert ein Dokument via LibreOffice.

        Args:
            input_file: Pfad zur Quelldatei (relativ zum Workspace oder absolut).
            output_format: Zielformat (pdf, docx, odt, html, txt).
            output_file: Optionaler Ausgabe-Pfad. Default: gleiches Verzeichnis, neue Extension.

        Returns:
            Pfad zur erzeugten Datei.

        Raises:
            FileNotFoundError: Wenn input_file nicht existiert.
            RuntimeError: Wenn die Konvertierung fehlschlaegt.
        """
        inp = self._resolve_path(input_file)
        if not inp.exists():
            raise FileNotFoundError(f"Datei nicht gefunden: {inp}")

        lo = shutil.which("libreoffice")
        if not lo:
            raise RuntimeError("LibreOffice nicht installiert.")

        out_dir = self._workspace if output_file is None else Path(output_file).parent
        out_dir.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            [lo, "--headless", "--convert-to", output_format, "--outdir", str(out_dir), str(inp)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"LibreOffice fehlgeschlagen: {result.stderr[:500]}")

        expected = out_dir / f"{inp.stem}.{output_format}"
        if not expected.exists():
            raise RuntimeError(f"Erwartete Ausgabedatei nicht gefunden: {expected}")

        if output_file and str(expected) != output_file:
            final = Path(output_file)
            expected.rename(final)
            return str(final)

        logger.info(f"Konvertiert: {inp.name} -> {expected.name}")
        return str(expected)

    def extract_pdf_text(self, pdf_file: str, ocr_fallback: bool = True) -> str:
        """Extrahiert Text aus einer PDF-Datei.

        Versucht zuerst pdftotext (schnell, fuer digitale PDFs).
        Falls leer und ocr_fallback=True, nutzt Tesseract OCR.

        Args:
            pdf_file: Pfad zur PDF-Datei.
            ocr_fallback: Bei leerer Extraktion auf OCR zurueckfallen.

        Returns:
            Extrahierter Text.
        """
        pdf = self._resolve_path(pdf_file)
        if not pdf.exists():
            raise FileNotFoundError(f"PDF nicht gefunden: {pdf}")

        text = ""

        # Versuch 1: pdftotext (Poppler)
        pdftotext = shutil.which("pdftotext")
        if pdftotext:
            result = subprocess.run(
                [pdftotext, "-layout", str(pdf), "-"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                text = result.stdout.strip()

        # Versuch 2: Tesseract OCR (fuer gescannte Dokumente)
        if not text and ocr_fallback and shutil.which("tesseract"):
            # PDF -> Bilder -> OCR (ueber pdftoppm + tesseract)
            pdftoppm = shutil.which("pdftoppm")
            if pdftoppm:
                import tempfile
                with tempfile.TemporaryDirectory() as tmpdir:
                    subprocess.run(
                        [pdftoppm, "-png", str(pdf), f"{tmpdir}/page"],
                        capture_output=True, timeout=120,
                    )
                    pages = sorted(Path(tmpdir).glob("page-*.png"))
                    parts = []
                    for page in pages:
                        ocr_result = subprocess.run(
                            ["tesseract", str(page), "stdout", "-l", "deu+eng"],
                            capture_output=True, text=True, timeout=60,
                        )
                        if ocr_result.returncode == 0:
                            parts.append(ocr_result.stdout.strip())
                    text = "\n\n".join(parts)

        logger.info(f"PDF-Text extrahiert: {pdf.name} ({len(text)} Zeichen)")
        return text

    def pandoc_convert(
        self,
        input_file: str,
        output_format: str,
        output_file: Optional[str] = None,
    ) -> str:
        """Konvertiert via Pandoc (Markdown, HTML, DOCX, etc.).

        Args:
            input_file: Quelldatei.
            output_format: Zielformat (html, docx, md, pdf, etc.).
            output_file: Optionaler Ausgabe-Pfad.

        Returns:
            Pfad zur erzeugten Datei.
        """
        pandoc = shutil.which("pandoc")
        if not pandoc:
            raise RuntimeError("Pandoc nicht installiert.")

        inp = self._resolve_path(input_file)
        if not inp.exists():
            raise FileNotFoundError(f"Datei nicht gefunden: {inp}")

        out = Path(output_file) if output_file else self._workspace / f"{inp.stem}.{output_format}"
        out.parent.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(
            [pandoc, str(inp), "-o", str(out)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Pandoc fehlgeschlagen: {result.stderr[:500]}")

        logger.info(f"Pandoc: {inp.name} -> {out.name}")
        return str(out)

    def compress(self, files: list[str], archive_name: str, fmt: str = "zip") -> str:
        """Erstellt ein Archiv aus den angegebenen Dateien.

        Args:
            files: Liste von Dateipfaden (relativ zum Workspace oder absolut).
            archive_name: Name des Archivs (ohne Extension).
            fmt: Archivformat ('zip' oder '7z').

        Returns:
            Pfad zum erstellten Archiv.
        """
        resolved = [str(self._resolve_path(f)) for f in files]
        for r in resolved:
            if not Path(r).exists():
                raise FileNotFoundError(f"Datei nicht gefunden: {r}")

        if fmt == "zip":
            archive = self._workspace / f"{archive_name}.zip"
            result = subprocess.run(
                ["zip", "-j", str(archive)] + resolved,
                capture_output=True, text=True, timeout=120,
            )
        elif fmt == "7z":
            archive = self._workspace / f"{archive_name}.7z"
            result = subprocess.run(
                ["7z", "a", str(archive)] + resolved,
                capture_output=True, text=True, timeout=120,
            )
        else:
            raise ValueError(f"Unbekanntes Format: {fmt}")

        if result.returncode != 0:
            raise RuntimeError(f"Archivierung fehlgeschlagen: {result.stderr[:500]}")

        logger.info(f"Archiv erstellt: {archive.name}")
        return str(archive)

    def extract_archive(self, archive_file: str, dest_dir: Optional[str] = None) -> str:
        """Entpackt ein Archiv (ZIP, 7z) in den Workspace.

        Args:
            archive_file: Pfad zum Archiv.
            dest_dir: Zielverzeichnis (default: Workspace).

        Returns:
            Pfad zum Zielverzeichnis.
        """
        archive = self._resolve_path(archive_file)
        if not archive.exists():
            raise FileNotFoundError(f"Archiv nicht gefunden: {archive}")

        dest = Path(dest_dir) if dest_dir else self._workspace
        dest.mkdir(parents=True, exist_ok=True)

        suffix = archive.suffix.lower()
        if suffix == ".zip":
            result = subprocess.run(
                ["unzip", "-o", str(archive), "-d", str(dest)],
                capture_output=True, text=True, timeout=120,
            )
        elif suffix in (".7z",):
            result = subprocess.run(
                ["7z", "x", str(archive), f"-o{dest}", "-y"],
                capture_output=True, text=True, timeout=120,
            )
        else:
            raise ValueError(f"Unbekanntes Archivformat: {suffix}")

        if result.returncode != 0:
            raise RuntimeError(f"Entpacken fehlgeschlagen: {result.stderr[:500]}")

        logger.info(f"Archiv entpackt: {archive.name} -> {dest}")
        return str(dest)

    # ── Big-File-Strategy (CR-091) ──────────────────────────────────────────

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return len(text) // _CHARS_PER_TOKEN

    def _ensure_chunk_table(self):
        """Create file_chunk_summaries table in agent's memory.db if missing."""
        db_path = self.memory_db_path(self._agent_name)
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS file_chunk_summaries (
                filename TEXT NOT NULL,
                chunk INTEGER NOT NULL,
                total_chunks INTEGER,
                summary TEXT NOT NULL,
                file_size INTEGER,
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (filename, chunk)
            )
        """)
        conn.commit()
        conn.close()

    def _read_file_chunked(self, filename: str, chunk: int = 0) -> str:
        """Read a specific chunk of a large file."""
        if not filename:
            return "Fehler: 'filename' ist ein Pflichtfeld."
        if "/" in filename or "\\" in filename or ".." in filename:
            return f"Ungueltiger Dateiname: {filename}"

        target = self._workspace / filename
        if not target.is_file():
            return f"Datei nicht gefunden: {filename}"

        text = target.read_text(encoding="utf-8", errors="replace")
        total_chars = len(text)
        est_tokens = self._estimate_tokens(text)

        if est_tokens <= _BIG_FILE_THRESHOLD_TOKENS:
            return (
                f"Datei ist klein genug fuer direktes Lesen ({est_tokens} Token). "
                f"Nutze read_file statt read_file_chunked.\n\n{text}"
            )

        chunk_size_chars = _CHUNK_SIZE_TOKENS * _CHARS_PER_TOKEN
        total_chunks = (total_chars + chunk_size_chars - 1) // chunk_size_chars

        if chunk < 0 or chunk >= total_chunks:
            return f"Ungueltiger Chunk {chunk}. Datei hat {total_chunks} Chunks (0-{total_chunks - 1})."

        start = chunk * chunk_size_chars
        end = min(start + chunk_size_chars, total_chars)
        chunk_text = text[start:end]

        logger.info(f"[BIG-FILE] {filename}: chunk {chunk}/{total_chunks-1} ({len(chunk_text)} chars)")
        return (
            f"[Chunk {chunk}/{total_chunks - 1}] Datei: {filename} "
            f"({total_chars} Zeichen, ~{est_tokens} Token, {total_chunks} Chunks)\n"
            f"Zeichen {start}-{end}:\n\n"
            f"{chunk_text}\n\n"
            f"--- Ende Chunk {chunk} ---\n"
            f"WICHTIG: Erstelle jetzt eine Zusammenfassung dieses Chunks mit store_chunk_summary, "
            f"dann lies {'Chunk ' + str(chunk + 1) if chunk + 1 < total_chunks else 'keinen weiteren Chunk (letzter erreicht)'}."
        )

    def _store_chunk_summary(self, filename: str, chunk: int, summary: str) -> str:
        """Store a chunk summary in memory.db."""
        self._ensure_chunk_table()
        target = self._workspace / filename
        file_size = target.stat().st_size if target.is_file() else 0

        text = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
        total_chars = len(text)
        chunk_size_chars = _CHUNK_SIZE_TOKENS * _CHARS_PER_TOKEN
        total_chunks = max(1, (total_chars + chunk_size_chars - 1) // chunk_size_chars)

        db_path = self.memory_db_path(self._agent_name)
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute(
            "INSERT INTO file_chunk_summaries (filename, chunk, total_chunks, summary, file_size, updated_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(filename, chunk) DO UPDATE SET summary=?, total_chunks=?, file_size=?, updated_at=datetime('now')",
            (filename, chunk, total_chunks, summary, file_size, summary, total_chunks, file_size),
        )
        conn.commit()
        conn.close()
        logger.info(f"[BIG-FILE] Stored summary for {filename} chunk {chunk}/{total_chunks-1}")
        return f"Zusammenfassung gespeichert: {filename} Chunk {chunk}/{total_chunks - 1}"

    def _get_file_overview(self, filename: str) -> str:
        """Return all stored chunk summaries for a file."""
        self._ensure_chunk_table()
        db_path = self.memory_db_path(self._agent_name)
        conn = sqlite3.connect(str(db_path), timeout=5)
        rows = conn.execute(
            "SELECT chunk, total_chunks, summary, file_size, updated_at "
            "FROM file_chunk_summaries WHERE filename=? ORDER BY chunk",
            (filename,),
        ).fetchall()
        conn.close()

        if not rows:
            return f"Keine Chunk-Zusammenfassungen fuer '{filename}' vorhanden."

        total = rows[0][1] or "?"
        fsize = rows[0][3] or 0
        lines = [f"Datei: {filename} ({fsize} bytes, {total} Chunks, {len(rows)} zusammengefasst)\n"]
        for chunk_nr, _, summary, _, updated in rows:
            lines.append(f"[Chunk {chunk_nr}] {summary}")
        return "\n\n".join(lines)

    def _search_in_file(self, filename: str, query: str, context_lines: int = 3) -> str:
        """Search for keyword/regex in file, return matching passages with context."""
        if "/" in filename or "\\" in filename or ".." in filename:
            return f"Ungueltiger Dateiname: {filename}"

        target = self._workspace / filename
        if not target.is_file():
            return f"Datei nicht gefunden: {filename}"

        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()

        # Compile regex (fall back to literal search if invalid pattern)
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error:
            pattern = re.compile(re.escape(query), re.IGNORECASE)

        matches = []
        for i, line in enumerate(lines):
            if pattern.search(line):
                matches.append(i)

        if not matches:
            return f"Keine Treffer fuer '{query}' in {filename} ({len(lines)} Zeilen)."

        # Build passages with context, merging overlapping ranges
        passages = []
        used = set()
        for match_line in matches:
            if match_line in used:
                continue
            start = max(0, match_line - context_lines)
            end = min(len(lines), match_line + context_lines + 1)
            passage_lines = []
            for j in range(start, end):
                marker = ">>>" if j == match_line else "   "
                passage_lines.append(f"{marker} {j + 1:>5}: {lines[j]}")
                used.add(j)
            passages.append("\n".join(passage_lines))
            if len(passages) >= 20:
                break

        result = (
            f"Suche '{query}' in {filename}: {len(matches)} Treffer "
            f"(zeige {len(passages)} Passagen, {context_lines} Kontextzeilen)\n\n"
        )
        result += "\n---\n".join(passages)
        if len(matches) > 20:
            result += f"\n\n... und {len(matches) - 20} weitere Treffer."

        logger.info(f"[SEARCH] {filename}: '{query}' → {len(matches)} hits")
        return result

    # ── Interne Hilfsfunktionen ──────────────────────────────────────────────

    def _resolve_path(self, file_path: str) -> Path:
        """Loest einen Dateipfad relativ zum Workspace auf.
        CR-214: Absolute paths are confined to storage/ to prevent
        agents from reading/writing arbitrary system files."""
        # Strip null bytes and control chars
        file_path = file_path.replace("\x00", "").strip()
        p = Path(file_path)
        if p.is_absolute():
            # Only allow paths under storage/ or the workspace itself
            resolved = p.resolve()
            storage_root = Path("storage").resolve()
            ws_root = self._workspace.resolve()
            if not (str(resolved).startswith(str(storage_root))
                    or str(resolved).startswith(str(ws_root))):
                # Confine to workspace
                return self._workspace / p.name
            return p
        # Prevent path traversal in relative paths
        clean = Path(*[part for part in p.parts if part != ".."])
        return self._workspace / clean
