"""Cheap local PDF helpers (text extraction, page counts, splitting). No API calls here."""
from __future__ import annotations

import math
from pathlib import Path
from typing import List

from pypdf import PdfReader, PdfWriter


def extract_text(pdf_path: Path) -> str:
    """Best-effort plain-text extraction, used only for the lightweight
    full-course context digest (not for the detailed slide-by-slide pass,
    which sends the native PDF to Gemini instead)."""
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as e:
        return f"[Could not read {pdf_path.name}: {e}]"

    parts = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if text.strip():
            parts.append(f"--- page {i} ---\n{text.strip()}")
    return "\n\n".join(parts)


def split_pdf(pdf_path: Path, parts: int, out_dir: Path) -> List[Path]:
    """Writes `pdf_path` as up to `parts` consecutive page-range PDFs into
    `out_dir` and returns their paths in page order."""
    reader = PdfReader(str(pdf_path))
    total = len(reader.pages)
    size = max(1, math.ceil(total / parts))
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks = []
    for start in range(0, total, size):
        end = min(start + size, total)
        writer = PdfWriter()
        for page in reader.pages[start:end]:
            writer.add_page(page)
        out = out_dir / f"{pdf_path.stem}_p{start + 1}-{end}.pdf"
        writer.write(out)
        chunks.append(out)
    return chunks


def page_count(pdf_path: Path) -> int:
    try:
        return len(PdfReader(str(pdf_path)).pages)
    except Exception:
        return 0
