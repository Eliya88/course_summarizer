"""Cheap local PDF helpers (text extraction, page counts, splitting). No API calls here."""
from __future__ import annotations

import math
from collections import Counter
from functools import lru_cache
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


@lru_cache(maxsize=None)
def _template_images(pdf_path: str) -> frozenset:
    """Image bytes repeated on most pages of the deck: a slide-template
    background or logo, never the figure a slide is about."""
    pages = PdfReader(pdf_path).pages
    counts = Counter(d for p in pages for d in {i.data for i in p.images})
    return frozenset(d for d, c in counts.items() if c > max(2, len(pages) / 2))


def extract_main_image(pdf_path: Path, page: int, out_dir: Path) -> Optional[Path]:
    """Saves the figure on 1-based `page` into `out_dir` and returns its
    path: the largest embedded raster image, or else a render of the
    vector-drawn chart/diagram (see _render_vector_figure). None if the page
    has neither (or can't be read)."""
    if page < 1:
        return None
    try:
        images = PdfReader(str(pdf_path)).pages[page - 1].images
        background = _template_images(str(pdf_path))
        img = max((i for i in images if len(i.data) >= MIN_IMAGE_BYTES and i.data not in background),
                  key=lambda i: len(i.data), default=None)
        out_dir.mkdir(parents=True, exist_ok=True)
        if img is None:
            return _render_vector_figure(pdf_path, page, out_dir / f"{pdf_path.stem}_p{page}_vec.png")
        out = out_dir / f"{pdf_path.stem}_p{page}{Path(img.name).suffix or '.png'}"
        out.write_bytes(img.data)
        return out
    except Exception:
        return None


# A chart or diagram drawn as vectors (e.g. a PowerPoint/Excel chart) is
# dozens of path objects; a slide's text decorations (fraction bars,
# underlines) are a handful.
MIN_VECTOR_PATHS = 20
_RENDER_SCALE = 3  # 216 dpi
_CROP_PAD_PT = 12


def _render_vector_figure(pdf_path: Path, page: int, out: Path) -> Optional[Path]:
    """Renders the area the page's vector paths cover to `out`, or returns
    None if there are too few paths to be a figure. Paths spanning nearly
    the whole page are backgrounds/frames and don't count. Text and images
    overlapping that area are included whole, so labels aren't cut."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        pg = doc[page - 1]
        w, h = pg.get_size()
        paths, others = [], []
        for obj in pg.get_objects(max_depth=3):
            box = obj.get_bounds()
            if (box[2] - box[0]) * (box[3] - box[1]) >= 0.8 * w * h:
                continue
            if obj.type == pdfium_c.FPDF_PAGEOBJ_PATH:
                paths.append(box)
            elif obj.type in (pdfium_c.FPDF_PAGEOBJ_TEXT, pdfium_c.FPDF_PAGEOBJ_IMAGE):
                others.append(box)
        if len(paths) < MIN_VECTOR_PATHS:
            return None
        l, b = min(x[0] for x in paths), min(x[1] for x in paths)
        r, t = max(x[2] for x in paths), max(x[3] for x in paths)
        for ol, ob, or_, ot in others:
            if ol < r and or_ > l and ob < t and ot > b:
                l, b, r, t = min(l, ol), min(b, ob), max(r, or_), max(t, ot)
        left, bottom = max(0, l - _CROP_PAD_PT), max(0, b - _CROP_PAD_PT)
        right, top = min(w, r + _CROP_PAD_PT), min(h, t + _CROP_PAD_PT)
        bitmap = pg.render(scale=_RENDER_SCALE, crop=(left, bottom, w - right, h - top))
        bitmap.to_pil().save(out)
        return out
    finally:
        doc.close()


def page_count(pdf_path: Path) -> int:
    try:
        return len(PdfReader(str(pdf_path)).pages)
    except Exception:
        return 0
