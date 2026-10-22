"""Cheap local PDF helpers (text extraction, page counts, splitting). No API calls here."""
from __future__ import annotations

import math
from pathlib import Path
from typing import List, Optional

from pypdf import PdfReader, PdfWriter

# Embedded images smaller than this are logos/icons, not figures worth showing.
MIN_IMAGE_BYTES = 20_000


def extract_text(pdf_path: Path) -> str:
    """Best-effort plain-text extraction, used only for the lightweight
    full-course context digest (not for the detailed slide-by-slide pass,
    which sends the native PDF to Claude instead)."""
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


def extract_main_image(pdf_path: Path, page: int, out_dir: Path) -> Optional[Path]:
    """Saves the largest embedded raster image on 1-based `page` into
    `out_dir` and returns its path, or None if the page has no image big
    enough to be a real figure (or can't be read). Vector-drawn diagrams
    have no embedded image, so they're not picked up."""
    # ponytail: picks by size only - a big slide-template background would win
    # over the figure; skip images repeated on most pages if that shows up.
    if page < 1:
        return None
    try:
        images = PdfReader(str(pdf_path)).pages[page - 1].images
        img = max((i for i in images if len(i.data) >= MIN_IMAGE_BYTES), key=lambda i: len(i.data), default=None)
        if img is None:
            return None
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{pdf_path.stem}_p{page}{Path(img.name).suffix or '.png'}"
        out.write_bytes(img.data)
        return out
    except Exception:
        return None


def page_count(pdf_path: Path) -> int:
    try:
        return len(PdfReader(str(pdf_path)).pages)
    except Exception:
        return 0
