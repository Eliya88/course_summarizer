"""Cheap local PDF helpers (text extraction, page counts, splitting). No API calls here."""
from __future__ import annotations

import math
from pathlib import Path
from typing import List, Optional, Tuple

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
        chunks.append(extract_pages(reader, start + 1, end, out_dir / f"{pdf_path.stem}_p{start + 1}-{end}.pdf"))
    return chunks


def extract_pages(reader: PdfReader, first: int, last: int, out: Path) -> Path:
    """Writes 1-based pages `first`..`last` (inclusive) of `reader` to `out`."""
    writer = PdfWriter()
    for page in reader.pages[first - 1:last]:
        writer.add_page(page)
    writer.write(out)
    return out


def chapter_ranges(reader: PdfReader) -> List[Tuple[str, int, int]]:
    """(title, first page, last page), 1-based, for every chapter bookmark.
    Chapters are the second-level bookmarks when the top level holds parts
    ("I Large Language Models" -> "Introduction", ...), else the top level.
    A chapter ends right before the next bookmark of either level, so it
    never runs into the next part's title page. [] if there are no bookmarks."""
    # ponytail: relies on the PDF's bookmarks - add a --pages fallback if a
    # book without them shows up.
    outline = reader.outline
    nested = any(isinstance(o, list) for o in outline)
    marks = []  # (is_chapter, title, start page)
    for o in outline:
        for child in (o if isinstance(o, list) else [o]):
            if not isinstance(child, list):
                is_chapter = isinstance(o, list) or not nested
                marks.append((is_chapter, child.title, reader.get_destination_page_number(child) + 1))
    starts = sorted({start for _, _, start in marks})
    total = len(reader.pages)
    return [
        (title, start, next((s - 1 for s in starts if s > start), total))
        for is_chapter, title, start in marks if is_chapter
    ]


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
