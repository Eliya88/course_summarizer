import sys
from pathlib import Path

import pytest
from fpdf import FPDF
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import config, pipeline, prompts


def make_book(tmp_path, bookmarks=True):
    """9 pages: front matter, part I (ch1 p3-4, ch2 p5), part II (ch3 p7-9)."""
    layout = [None, (0, "I Part One"), (1, "Intro"), None, (1, "Words"),
              (0, "II Part Two"), (1, "Embeddings"), None, None]
    pdf = FPDF()
    pdf.set_font("helvetica", size=12)
    for i, mark in enumerate(layout, start=1):
        pdf.add_page()
        if mark and bookmarks:
            pdf.start_section(mark[1], level=mark[0])
        pdf.cell(text=f"page {i}")
    out = tmp_path / "book.pdf"
    pdf.output(str(out))
    return out


def test_chapter_ranges_uses_chapters_inside_parts(tmp_path):
    reader = PdfReader(str(make_book(tmp_path)))
    assert config.pdf_utils.chapter_ranges(reader) == [("Intro", 3, 4), ("Words", 5, 5), ("Embeddings", 7, 9)]


def test_chapter_ranges_without_bookmarks(tmp_path):
    assert config.pdf_utils.chapter_ranges(PdfReader(str(make_book(tmp_path, bookmarks=False)))) == []


def test_parse_chapters():
    assert config.parse_chapters("1-3") == [1, 2, 3]
    assert config.parse_chapters("2") == [2]
    assert config.parse_chapters("3-4, 1") == [1, 3, 4]
    with pytest.raises(ValueError):
        config.parse_chapters("x")


def test_discover_book_config(tmp_path):
    book = make_book(tmp_path)
    course = config.discover_book_config(book, [1, 3], output_root=tmp_path / "out")
    assert course.book and course.course_name == "book ch1,3"
    assert [u.name for u in course.units] == ["Chapter 1: Intro", "Chapter 3: Embeddings"]
    files = [p for u in course.units for p in u.source_paths(course)]
    assert [p.name for p in files] == ["ch01_p3-4.pdf", "ch03_p7-9.pdf"]
    assert [len(PdfReader(str(p)).pages) for p in files] == [2, 3]

    mtimes = [p.stat().st_mtime_ns for p in files]
    keys = [u.cache_key(course) for u in course.units]
    again = config.discover_book_config(book, [1, 3], output_root=tmp_path / "out")
    assert [p.stat().st_mtime_ns for p in files] == mtimes
    assert [u.cache_key(again) for u in again.units] == keys

    course.book = False
    assert course.units[0].cache_key(course) != keys[0]

    with pytest.raises(ValueError):
        config.discover_book_config(book, [4], output_root=tmp_path / "out")


def test_prompts_regular_unchanged_book_has_focus(tmp_path):
    course = config.discover_book_config(make_book(tmp_path), [1], output_root=tmp_path / "out")
    course.focus = "Implement BPE"
    book_sys = pipeline._system_instruction(course)
    assert "Implement BPE" in book_sys and "textbook chapter" in book_sys

    course.book, course.focus = False, ""
    regular_sys = pipeline._system_instruction(course)
    assert regular_sys.endswith("already provides one.")
    regular_sec = prompts.SECTION_PROMPT.format(n=1, topic_list="T", length_rule=prompts.SLIDE_LENGTH_RULE)
    assert "1.5 times" in regular_sec and "4-5 pages" not in regular_sec
