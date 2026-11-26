"""CLI entrypoint.

Usage:
    python main.py --course-dir "../NLP"                  # auto-discover, no YAML needed
    python main.py --course-dir "../NLP" --no-cache
    python main.py --course-dir "../NLP" --rebuild 3     # make lecture 3 again, the rest from the cache
    python main.py --course-dir "../NLP" --fresh-pdf     # don't carry the old PDF's highlights and notes over
    python main.py --course-dir "../NLP" --with-exercises # also summarize exercise/tutorial units
    python main.py --course-dir "../NLP" --with-tests     # add a past-exam practice page after each chapter
    python main.py --course courses/data_science.yaml     # or use a hand-written YAML config
    python main.py --book "book.pdf" --chapters 1-5 --focus "ex1.pdf"  # chapters of a textbook, focused on an assignment
    python main.py --list-pdfs "Data Science/Lectures"   # helper to write configs

Lecture transcripts (.txt) in a "Transcript(s)" folder anywhere in the course
folder are always used: "L1.txt" joins the lecture PDF starting with "L1", and
one with no matching PDF is summarized on its own. A leading "_" skips one.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

# Windows consoles default to cp1252, which can't encode Hebrew section
# titles Claude returns - force UTF-8 so progress prints don't crash mid-run.
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# pypdf warns about every font with a duplicated /ToUnicode entry (common in
# big textbooks); harmless, and it buries the progress output.
logging.getLogger("pypdf").setLevel(logging.ERROR)


from src.config import (attach_transcripts, discover_book_config, discover_course_config, load_course_config,
                        parse_chapters)
from src.keep_notes import carry_notes, count_notes
from src.pdf_builder import build_pdf
from src.pipeline import run


def list_pdfs(dir_path: str) -> None:
    p = Path(dir_path)
    if not p.exists():
        print(f"No such directory: {p}")
        return
    for f in sorted(p.glob("*.pdf")):
        print(f"      - \"{f.name}\"")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build one summary PDF from a course's lectures + exercises.")
    parser.add_argument("--course-dir", type=str, help="Path to a course folder containing Lectures/Exercises subfolders - auto-discovers units, no YAML needed")
    parser.add_argument("--course", type=str, help="Path to a course YAML config, e.g. courses/data_science.yaml")
    parser.add_argument("--no-cache", action="store_true", help="Re-call Claude for every unit even if a cached result exists")
    parser.add_argument("--rebuild", type=str, help="Lecture (or chapter) numbers to make again, ignoring their cache, e.g. 3 or 2,4 - numbered as in the log")
    parser.add_argument("--with-exercises", action="store_true", help="Also summarize exercise/tutorial units (lectures only by default)")
    parser.add_argument("--with-tests", action="store_true", help="After each chapter, add a practice page with a question from the course's past exams and a worked solution (needs an exams folder in the course directory)")
    parser.add_argument("--book", type=str, help="Path to a textbook PDF with chapter bookmarks - summarizes the --chapters given, shorter than a regular run")
    parser.add_argument("--chapters", type=str, help="With --book: chapter numbers, e.g. 1-5 or 1,3,5")
    parser.add_argument("--focus", type=str, help="With --book: an assignment PDF the summary should go deep on")
    parser.add_argument("--fresh-pdf", action="store_true", help="Don't carry the highlights and notes made in the previous summary PDF into the new one")
    parser.add_argument("--instructions", type=str, help='Extra instructions for the model, e.g. "Explain unfamiliar words in a footnote"')
    parser.add_argument("--list-pdfs", type=str, metavar="DIR", help="List PDFs in DIR formatted for a course YAML, then exit")
    args = parser.parse_args()

    if args.list_pdfs:
        list_pdfs(args.list_pdfs)
        return

    if sum(map(bool, (args.course_dir, args.course, args.book))) != 1:
        parser.error("give exactly one of --course-dir, --course or --book (or use --list-pdfs to help write a config)")
    if args.book and not args.chapters:
        parser.error("--book needs --chapters, e.g. --chapters 1-5")
    if (args.chapters or args.focus) and not args.book:
        parser.error("--chapters and --focus only work with --book")

    load_dotenv()

    if args.course_dir:
        course = discover_course_config(Path(args.course_dir))
    elif args.book:
        course = discover_book_config(Path(args.book), args.chapters,
                                      Path(args.focus) if args.focus else None)
    else:
        course = load_course_config(Path(args.course))

    course.instructions = (args.instructions or "").strip()
    if not args.book and (n_transcripts := attach_transcripts(course)):
        print(f"  Transcripts: {n_transcripts} from {course.transcripts_dir}")

    if not args.with_exercises:
        excluded = [u for u in course.units if not (u.lecture_files or u.transcript_files)]
        course.units = [u for u in course.units if u.lecture_files or u.transcript_files]
        for u in excluded:
            print(f"  skipping exercise unit (pass --with-exercises to include): {u.name}")

    print(f"Course: {course.course_name}  |  model: {course.model}  |  units: {len(course.units)}")
    if args.with_tests:
        n_exams = len(course.exam_paths())
        print(f"  [--with-tests] flag: {n_exams} past-exam PDF(s) from "
              f"{course.exams_dir if course.exams_dir else 'no exams folder found'}")

    rebuild = frozenset(parse_chapters(args.rebuild)) if args.rebuild else frozenset()
    if unknown := sorted(rebuild - set(range(1, len(course.units) + 1))):
        parser.error(f"--rebuild {args.rebuild}: there are {len(course.units)} units, no {unknown[0]}")
    for n in sorted(rebuild):
        print(f"  [--rebuild] making unit {n} again: {course.units[n - 1].name}")

    sections = run(
        course,
        use_cache=not args.no_cache,
        with_tests=args.with_tests,
        rebuild=rebuild,
    )

    output_path = course.output_dir / f"{course.course_name.replace(' ', '_')}_summary.pdf"
    copy_to = course.root_dir / output_path.name if (args.course_dir or args.book) else None
    # The copy the reader opens (next to the course) is where highlights get saved.
    previous = _back_up_notes(copy_to or output_path, course.output_dir) if not args.fresh_pdf else None

    print("Building the PDF...")
    build_pdf(
        sections,
        output_path,
        course_name=course.course_name,
        chapter_titles=[u.name for u in course.units],
    )
    if previous:
        _carry_notes(previous, output_path)
    print(f"\nDone. Wrote: {output_path}")
    if copy_to:
        try:
            shutil.copy2(output_path, copy_to)
        except PermissionError:
            sys.exit(f"Couldn't replace {copy_to} - is it open in a PDF reader? "
                     f"Close it and run again (the cache makes that quick), or copy the new PDF from {output_path}")
        print(f"Copied to: {copy_to}")


def _back_up_notes(pdf_path: Path, backup_dir: Path) -> Path | None:
    """Saves a copy of the current summary if it has highlights or notes, to carry them over from."""
    if not pdf_path.exists() or not (n := count_notes(pdf_path)):
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{pdf_path.stem}_previous.pdf"
    shutil.copy2(pdf_path, backup)
    print(f"  Your highlights and notes: {n} in {pdf_path.name}, saved a copy as {backup}")
    return backup


def _carry_notes(previous: Path, output_path: Path) -> None:
    try:
        result = carry_notes(previous, output_path)
    except Exception as e:  # never lose the new PDF over this
        print(f"  Couldn't carry your highlights over ({e}). They're still in {previous}")
        return
    print(f"  Carried {result.kept} highlights and notes into the new PDF.")
    if result.lost_pages:
        pages = ", ".join(map(str, sorted(set(result.lost_pages))))
        print(f"  {len(result.lost_pages)} couldn't be placed - their text changed (old pages {pages}). "
              f"They're still in {previous}")


if __name__ == "__main__":
    main()
