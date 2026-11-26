from pathlib import Path

import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Highlight
from pypdf.generic import ArrayObject, FloatObject

from src import keep_notes
from src.pdf_builder import build_pdf


def _chapter(name: str, lines: int = 6) -> str:
    return f"## {name}\n\n" + "\n\n".join(f"Line {i} of {name}, worded so it appears only once." for i in range(lines))


def _build(path: Path, chapters: list) -> Path:
    sections = ["# Course\n\n" + "\n".join(f"@@TOC@@ {i}|{c}" for i, c in enumerate(chapters, 1))]
    sections += [_chapter(c) for c in chapters]
    return build_pdf(sections, path, chapter_titles=chapters)


def _highlight(pdf: Path, page: int, text: str) -> None:
    """Highlights the line holding `text`, the way a PDF reader saves one."""
    line = next(ln for ln in keep_notes._all_lines(pdf)[page] if text.replace(" ", "") in ln.key)
    writer = PdfWriter(clone_from=pdf)
    rect = (60, line.bottom, 540, line.top)
    note = Highlight(rect=rect, quad_points=ArrayObject(
        FloatObject(v) for v in (60, line.top, 540, line.top, 60, line.bottom, 540, line.bottom)))
    writer.add_annotation(page, note)
    writer.write(pdf)


def _text_under_notes(pdf: Path) -> list:
    doc, out = pdfium.PdfDocument(str(pdf)), []
    for p, page in enumerate(PdfReader(pdf).pages):
        for a in page.get("/Annots") or []:
            a = a.get_object()
            if a["/Subtype"] == "/Highlight":
                x0, y0, x1, y1 = (float(v) for v in a["/Rect"])
                out.append((p, doc[p].get_textpage().get_text_bounded(x0, y0 + 2, x1, y1 - 2).strip()))
    return out


def test_a_highlight_follows_its_line_when_a_lecture_is_added_before_it(tmp_path):
    old = _build(tmp_path / "old.pdf", ["Alpha", "Beta"])
    _highlight(old, 2, "Line 3 of Beta")
    assert keep_notes.count_notes(old) == 1  # the cover's chapter links aren't notes

    new = _build(tmp_path / "new.pdf", ["Alpha", "Inserted", "Beta"])
    result = keep_notes.carry_notes(old, new)

    assert (result.kept, result.lost_pages) == (1, [])
    [(page, text)] = _text_under_notes(new)
    assert page == 3 and "Line 3 of Beta, worded so it appears only once" in text


def test_a_highlight_on_reworded_text_is_reported_not_misplaced(tmp_path):
    old = _build(tmp_path / "old.pdf", ["Alpha", "Beta"])
    _highlight(old, 2, "Line 3 of Beta")

    new = _build(tmp_path / "new.pdf", ["Alpha", "Gamma"])
    result = keep_notes.carry_notes(old, new)

    assert (result.kept, result.lost_pages) == (0, [3])
    assert keep_notes.count_notes(new) == 0
