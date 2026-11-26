"""Carries highlights and notes made in a PDF reader from the previous summary
into a rebuilt one.

A rebuild writes a new file, so the reader's annotations in the old one would
be lost. Each annotation is anchored to the text lines or figure it covers
(or the nearest line, for a pen stroke in the margin). The same lines are looked up in
the new PDF: on the same page when nothing before them changed, or on a later
page when a lecture was added before them or a heading moved. The annotation
is copied there, shifted by the distance its lines moved. Lines whose text
changed (a lecture made again, say) can't be found, so annotations on them are
left out and their pages reported.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, FloatObject, NameObject

# Annotations the app draws itself (links) or that only belong to another
# annotation (a note's pop-up window, copied along with its owner).
_NOT_NOTES = {"/Link", "/Popup", "/Widget"}
# Running chapter name above, page number below: they differ between builds,
# so lines there never anchor anything. Points from the page's bottom/top.
_FOOTER_BAND, _HEADER_BAND = 45, 62
# A single short line ("נושאים:", "1.") repeats across the PDF, so it only
# anchors a note when a neighbouring line matches too.
_MIN_ALONE_CHARS = 12


@dataclass
class _Line:
    key: str  # the line's text without whitespace
    bottom: float
    top: float


@dataclass
class Result:
    kept: int
    lost_pages: List[int]  # 1-based pages of the previous PDF, one per note left out


def count_notes(pdf_path: Path) -> int:
    try:
        reader = PdfReader(pdf_path)
        return sum(1 for page in reader.pages for a in page.get("/Annots") or []
                   if a.get_object().get("/Subtype") not in _NOT_NOTES)
    except Exception:
        return 0


def _page_lines(page: pdfium.PdfPage) -> List[_Line]:
    """The page's body text lines and figures, top to bottom."""
    height = page.get_size()[1]
    textpage = page.get_textpage()
    chars = []  # (index, char, bottom, top)
    for i in range(textpage.count_chars()):
        ch = chr(pdfium_c.FPDFText_GetUnicode(textpage.raw, i))
        if ch.isspace() or ch == "\x00":
            continue
        _, bottom, _, top = textpage.get_charbox(i, loose=True)
        if top > bottom and _FOOTER_BAND < (bottom + top) / 2 < height - _HEADER_BAND:
            chars.append((i, ch, bottom, top))
    textpage.close()

    # Loose boxes give every character on a line the same height, so a line is
    # the run of characters whose centres fall inside the first one's box.
    chars.sort(key=lambda c: -(c[2] + c[3]))
    groups: List[list] = []
    for c in chars:
        if groups and groups[-1][0][2] <= (c[2] + c[3]) / 2 <= groups[-1][0][3]:
            groups[-1].append(c)
        else:
            groups.append([c])
    lines = [_Line(key="".join(c[1] for c in sorted(g)),
                   bottom=min(c[2] for c in g), top=max(c[3] for c in g)) for g in groups]

    # A slide figure counts as one "line", keyed by its image data, so a pen
    # stroke drawn on a figure (maybe on a page with no text) follows it.
    for image in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]):
        _, bottom, _, top = image.get_bounds()
        digest = hashlib.sha1(bytes(image.get_data(decode_simple=False))).hexdigest()[:16]
        lines.append(_Line(key=f"image:{digest}", bottom=bottom, top=top))
    return sorted(lines, key=lambda ln: -ln.top)


def _all_lines(pdf_path: Path) -> List[List[_Line]]:
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        return [_page_lines(doc[i]) for i in range(len(doc))]
    finally:
        doc.close()


def _anchor(lines: List[_Line], rect: List[float]) -> Optional[Tuple[int, int]]:
    """Index range [first, last] of the lines a note covers, else the nearest line."""
    y0, y1 = min(rect[1], rect[3]), max(rect[1], rect[3])
    covered = [i for i, ln in enumerate(lines)
               if min(ln.top, y1) - max(ln.bottom, y0) >= 0.5 * min(ln.top - ln.bottom, y1 - y0)]
    if covered:
        return covered[0], covered[-1]
    if not lines:
        return None
    mid = (y0 + y1) / 2
    i = min(range(len(lines)), key=lambda k: abs((lines[k].bottom + lines[k].top) / 2 - mid))
    return i, i


def _find(old: List[_Line], first: int, last: int, new_pages: List[List[_Line]],
          index: Dict[str, List[Tuple[int, int]]], expected_page: int) -> Optional[Tuple[int, float]]:
    """(new page, vertical shift) where the old lines first..last appear again."""
    keys = [ln.key for ln in old[first:last + 1]]
    before = old[first - 1].key if first > 0 else None
    after = old[last + 1].key if last + 1 < len(old) else None
    best = None
    for page, j in index.get(keys[0], ()):
        lines = new_pages[page]
        if [ln.key for ln in lines[j:j + len(keys)]] != keys:
            continue
        context = ((before is not None and j > 0 and lines[j - 1].key == before)
                   + (after is not None and j + len(keys) < len(lines) and lines[j + len(keys)].key == after))
        if not context and sum(map(len, keys)) < _MIN_ALONE_CHARS:
            continue
        shift = lines[j].top - old[first].top
        score = (-context, abs(page - expected_page), abs(shift))
        if best is None or score < best[0]:
            best = (score, page, shift)
    return (best[1], best[2]) if best else None


def _shift(obj, dy: float) -> None:
    """Moves an annotation's coordinates (every y in its point lists) by dy."""
    def moved(points):
        return ArrayObject(FloatObject(float(v) + (dy if k % 2 else 0)) for k, v in enumerate(points))

    for key in ("/Rect", "/QuadPoints", "/Vertices", "/L", "/CL"):
        if key in obj:
            obj[NameObject(key)] = moved(obj[key])
    if "/InkList" in obj:
        obj[NameObject("/InkList")] = ArrayObject(moved(stroke) for stroke in obj["/InkList"])


def carry_notes(old_pdf: Path, new_pdf: Path) -> Result:
    """Copies old_pdf's highlights and notes into new_pdf, in place."""
    reader = PdfReader(old_pdf)
    notes = [(p, a) for p, page in enumerate(reader.pages) for a in page.get("/Annots") or []
             if a.get_object().get("/Subtype") not in _NOT_NOTES]
    if not notes:
        return Result(0, [])

    old_pages, new_pages = _all_lines(old_pdf), _all_lines(new_pdf)
    index: Dict[str, List[Tuple[int, int]]] = {}
    for p, lines in enumerate(new_pages):
        for j, ln in enumerate(lines):
            index.setdefault(ln.key, []).append((p, j))

    writer = PdfWriter(clone_from=new_pdf)
    page_offset, kept, lost, copies = 0, 0, [], {}
    for p, ref in notes:
        note = ref.get_object()
        anchor = _anchor(old_pages[p], [float(v) for v in note["/Rect"]])
        found = anchor and _find(old_pages[p], *anchor, new_pages, index, p + page_offset)
        if not found:
            lost.append(p + 1)
            continue
        new_page, dy = found
        page_offset = new_page - p
        page = writer.pages[new_page]
        if "/Annots" not in page:
            page[NameObject("/Annots")] = ArrayObject()
        annots = page["/Annots"].get_object()

        copy = note.clone(writer, force_duplicate=True, ignore_fields=("/P", "/Popup", "/Parent", "/IRT"))
        _shift(copy, dy)
        copy[NameObject("/P")] = page.indirect_reference
        copy_ref = writer._add_object(copy)
        annots.append(copy_ref)
        copies[id(note)] = copy_ref
        if "/Popup" in note:  # the note's comment window
            popup = note["/Popup"].get_object().clone(writer, force_duplicate=True,
                                                      ignore_fields=("/P", "/Parent"))
            _shift(popup, dy)
            popup[NameObject("/P")] = page.indirect_reference
            popup[NameObject("/Parent")] = copy_ref
            copy[NameObject("/Popup")] = writer._add_object(popup)
            annots.append(copy["/Popup"])
        kept += 1

    # A reply to a note points at the note it answers.
    for p, ref in notes:
        note = ref.get_object()
        if "/IRT" in note and id(note) in copies and id(note["/IRT"].get_object()) in copies:
            copies[id(note)].get_object()[NameObject("/IRT")] = copies[id(note["/IRT"].get_object())]

    tmp = new_pdf.with_name(new_pdf.stem + ".notes.tmp.pdf")
    with open(tmp, "wb") as f:
        writer.write(f)
    os.replace(tmp, new_pdf)
    return Result(kept, lost)
