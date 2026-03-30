"""
CR-196: Document OCR Skill — PDF/Image → Text
===============================================
3-tier extraction with automatic escalation:

Tier 1: Direct text extraction (pdfplumber) — fast, local, no GPU
Tier 2: Local OCR (Tesseract on rendered pages) — slower, local, no GPU
Tier 3: External API (Claude Vision on page images) — best quality, API cost

The skill auto-escalates: if Tier 1 yields <50 chars/page, try Tier 2.
If Tier 2 yields <50 chars/page, escalate to Tier 3 (external API).

Tools:
  read_pdf(filename, pages, question) — Extract text from PDF in workspace
"""

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from core.skills.base import BaseSkill

logger = logging.getLogger("DocumentOCR")

_MIN_CHARS_PER_PAGE = 50  # Below this → escalate to next tier


class DocumentOCRSkill(BaseSkill):
    """PDF and document text extraction with OCR fallback."""

    SKILL_NAME = "document_ocr"

    def __init__(self, agent_name: str, config: dict,
                 secrets: dict[str, str] | None = None, **kwargs):
        self._init_secrets(secrets)
        self._agent_name = agent_name
        self._config = config

    @classmethod
    def is_available(cls, config: dict | None = None) -> bool:
        try:
            import pdfplumber
            return True
        except ImportError:
            return False

    @classmethod
    def credentials_schema(cls) -> list[dict]:
        return []  # No credentials needed — all local except Tier 3

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "read_pdf",
                "description": (
                    "Liest Text aus einer PDF-Datei im Workspace. "
                    "Verwendet automatisch OCR wenn der Text nicht direkt extrahierbar ist. "
                    "Bei schlechter Qualitaet wird die externe API fuer Bildanalyse genutzt."
                ),
                "parameters": {
                    "filename": {"type": "string", "description": "PDF-Dateiname im Workspace", "required": True},
                    "pages": {"type": "string", "description": "Seitenbereich z.B. '1-3' oder 'all' (default: all)", "default": "all"},
                    "question": {"type": "string", "description": "Spezifische Frage zum Dokument", "default": ""},
                },
            },
        ]

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "read_pdf":
            filename = arguments.get("filename", "")
            pages_str = arguments.get("pages", "all")
            question = arguments.get("question", "")
            workspace = self.workspace_path(self._agent_name)
            # Check workspace root and public/
            pdf_path = workspace / filename
            if not pdf_path.exists():
                pdf_path = workspace / "public" / filename
            if not pdf_path.exists():
                return f"Fehler: PDF nicht gefunden: {filename}"
            return await self._read_pdf(pdf_path, pages_str, question)
        return f"Unbekanntes Tool: {tool_name}"

    async def _read_pdf(self, pdf_path: Path, pages_str: str, question: str) -> str:
        """3-tier PDF text extraction."""
        import pdfplumber

        # Parse page range
        try:
            pdf = pdfplumber.open(str(pdf_path))
            total_pages = len(pdf.pages)
        except Exception as exc:
            return f"Fehler beim Oeffnen der PDF: {exc}"

        if pages_str == "all":
            page_indices = list(range(total_pages))
        else:
            page_indices = self._parse_pages(pages_str, total_pages)

        # Limit to 20 pages max
        if len(page_indices) > 20:
            page_indices = page_indices[:20]
            logger.warning(f"[OCR] Limiting to 20 pages (total: {total_pages})")

        # ── Tier 1: Direct text extraction ──
        logger.info(f"[OCR] Tier 1: pdfplumber on {pdf_path.name} ({len(page_indices)} pages)")
        texts = []
        low_quality_pages = []
        for i in page_indices:
            page = pdf.pages[i]
            text = (page.extract_text() or "").strip()
            texts.append(text)
            if len(text) < _MIN_CHARS_PER_PAGE:
                low_quality_pages.append(i)

        pdf.close()

        if not low_quality_pages:
            # All pages extracted fine
            result = self._format_pages(texts, page_indices)
            logger.info(f"[OCR] Tier 1 success: {len(result)} chars from {len(page_indices)} pages")
            return result

        # ── Tier 2: Local OCR (Tesseract) ──
        logger.info(f"[OCR] Tier 2: Tesseract OCR on {len(low_quality_pages)} low-quality pages")
        ocr_failed_pages = []
        try:
            import pytesseract
            from pdf2image import convert_from_path

            images = convert_from_path(
                str(pdf_path),
                first_page=min(low_quality_pages) + 1,
                last_page=max(low_quality_pages) + 1,
                dpi=300,
            )

            img_idx = 0
            for page_num in low_quality_pages:
                if img_idx < len(images):
                    ocr_text = pytesseract.image_to_string(images[img_idx], lang="deu+eng").strip()
                    if len(ocr_text) >= _MIN_CHARS_PER_PAGE:
                        idx = page_indices.index(page_num)
                        texts[idx] = ocr_text
                    else:
                        ocr_failed_pages.append(page_num)
                    img_idx += 1
                else:
                    ocr_failed_pages.append(page_num)

        except Exception as exc:
            logger.warning(f"[OCR] Tier 2 failed: {exc}")
            ocr_failed_pages = low_quality_pages

        if not ocr_failed_pages:
            result = self._format_pages(texts, page_indices)
            logger.info(f"[OCR] Tier 2 success: {len(result)} chars")
            return result

        # ── Tier 3: External API (Claude Vision) ──
        logger.info(f"[OCR] Tier 3: Claude Vision on {len(ocr_failed_pages)} pages")
        try:
            from core.skills.skill_hybrid_reasoning import HybridReasoningSkill
            hr = HybridReasoningSkill(self._agent_name, self._config,
                                       secrets=getattr(self, "_secrets", {}))
            if not hr.is_available():
                logger.warning("[OCR] Tier 3 unavailable — no API key")
            else:
                from pdf2image import convert_from_path
                for page_num in ocr_failed_pages[:5]:  # Max 5 pages via API
                    try:
                        imgs = convert_from_path(str(pdf_path), first_page=page_num+1,
                                                 last_page=page_num+1, dpi=200)
                        if imgs:
                            # Save temp image
                            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                                imgs[0].save(tmp, format="JPEG", quality=85)
                                tmp_path = tmp.name

                            q = question or "Extrahiere allen sichtbaren Text aus diesem Dokument."
                            vision_text = await hr.analyze_image(tmp_path, q)
                            os.unlink(tmp_path)

                            if vision_text and len(vision_text) > 20:
                                idx = page_indices.index(page_num)
                                texts[idx] = vision_text
                                logger.info(f"[OCR] Tier 3 page {page_num+1}: {len(vision_text)} chars")
                    except Exception as exc:
                        logger.warning(f"[OCR] Tier 3 page {page_num+1} failed: {exc}")

        except Exception as exc:
            logger.warning(f"[OCR] Tier 3 setup failed: {exc}")

        result = self._format_pages(texts, page_indices)
        logger.info(f"[OCR] Final result: {len(result)} chars from {len(page_indices)} pages")
        return result

    def _format_pages(self, texts: list[str], page_indices: list[int]) -> str:
        parts = []
        for text, page_num in zip(texts, page_indices):
            if text.strip():
                parts.append(f"--- Seite {page_num + 1} ---\n{text}")
        return "\n\n".join(parts) if parts else "(Kein Text extrahierbar)"

    def _parse_pages(self, pages_str: str, total: int) -> list[int]:
        """Parse '1-3' or '1,3,5' into 0-based indices."""
        indices = []
        for part in pages_str.split(","):
            part = part.strip()
            if "-" in part:
                start, end = part.split("-", 1)
                for i in range(int(start) - 1, min(int(end), total)):
                    indices.append(i)
            else:
                i = int(part) - 1
                if 0 <= i < total:
                    indices.append(i)
        return indices or list(range(min(total, 5)))
