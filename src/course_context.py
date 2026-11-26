"""Builds a lightweight, plain-text digest of every lecture + exercise PDF
in the course. This is sent as text context on every Claude call so the
model always has the *breadth* of the whole course, even while it's doing a
*deep* slide-by-slide pass on one unit's native PDF(s)."""
from __future__ import annotations

from pathlib import Path
from typing import List

from .config import CourseConfig, fingerprint
from .pdf_utils import extract_text


def _all_source_pdfs(course: CourseConfig) -> List[Path]:
    """Only files actually referenced by a unit in the config - not every
    PDF sitting in the lectures/exercises folders. Course folders often
    contain other clutter (merged/duplicate files, assignments, unrelated
    material) that shouldn't be silently folded into every API call."""
    seen = []
    for unit in course.units:
        for p in unit.source_paths(course):
            if p.exists() and p not in seen:
                seen.append(p)
    return sorted(seen, key=lambda p: p.name)


def build_course_context(course: CourseConfig) -> str:
    """Returns the digest text, rebuilding it only if source PDFs changed."""
    pdfs = _all_source_pdfs(course)
    cache_path = course.output_dir / "cache" / f"course_context_{fingerprint(pdfs)[:16]}.md"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")

    print(f"[course_context] Extracting text from {len(pdfs)} PDF(s) for course digest...")
    sections = [f"# Full course material index: {course.course_name}\n"]
    for pdf in pdfs:
        print(f"  - {pdf.name}")
        text = extract_text(pdf)
        sections.append(f"\n## Source file: {pdf.name}\n\n{text}\n")

    digest = "\n".join(sections)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(digest, encoding="utf-8")

    return digest
