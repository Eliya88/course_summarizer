"""CLI entrypoint.

Usage:
    python main.py --course-dir "../NLP"                  # auto-discover, no YAML needed
    python main.py --course-dir "../NLP" --no-cache
    python main.py --course-dir "../NLP" --no-exercises   # lectures only, skip exercise/tutorial units
    python main.py --course-dir "../NLP" --with-tests     # add a past-exam practice page after each chapter
    python main.py --course courses/data_science.yaml     # or use a hand-written YAML config
    python main.py --course courses/data_science.yaml --refresh-context
    python main.py --list-pdfs "Data Science/Lectures"   # helper to write configs
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# Windows consoles default to cp1252, which can't encode Hebrew section
# titles Claude returns - force UTF-8 so progress prints don't crash mid-run.
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

sys.path.insert(0, str(Path(__file__).parent))

from src.config import discover_course_config, load_course_config
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
    parser = argparse.ArgumentParser(description="Build one exam-prep PDF from a course's lectures + exercises.")
    parser.add_argument("--course-dir", type=str, help="Path to a course folder containing Lectures/Exercises subfolders - auto-discovers units, no YAML needed")
    parser.add_argument("--course", type=str, help="Path to a course YAML config, e.g. courses/data_science.yaml")
    parser.add_argument("--no-cache", action="store_true", help="Re-call Claude for every unit even if a cached result exists")
    parser.add_argument("--refresh-context", action="store_true", help="Rebuild the full-course text digest even if unchanged")
    parser.add_argument("--no-exercises", action="store_true", help="Exclude exercise/tutorial units from the summary (included by default)")
    parser.add_argument("--with-tests", action="store_true", help="After each chapter, add a practice page with a question from the course's past exams and a worked solution (needs an exams folder in the course directory)")
    parser.add_argument("--list-pdfs", type=str, metavar="DIR", help="List PDFs in DIR formatted for a course YAML, then exit")
    args = parser.parse_args()

    if args.list_pdfs:
        list_pdfs(args.list_pdfs)
        return

    if not args.course_dir and not args.course:
        parser.error("--course-dir or --course is required (or use --list-pdfs to help write a config)")

    load_dotenv()

    if args.course_dir:
        course = discover_course_config(Path(args.course_dir))
    else:
        course = load_course_config(Path(args.course))

    if args.no_exercises:
        excluded = [u for u in course.units if not u.lecture_files]
        course.units = [u for u in course.units if u.lecture_files]
        for u in excluded:
            print(f"  [--no-exercises] flag: skipping exercise unit: {u.name}")

    print(f"Course: {course.course_name}  |  model: {course.model}  |  units: {len(course.units)}")
    if args.with_tests:
        n_exams = len(course.exam_paths())
        print(f"  [--with-tests] flag: {n_exams} past-exam PDF(s) from "
              f"{course.exams_dir if course.exams_dir else 'no exams folder found'}")

    sections = run(
        course,
        use_cache=not args.no_cache,
        force_context=args.refresh_context,
        with_tests=args.with_tests,
    )

    output_path = course.output_dir / f"{course.course_name.replace(' ', '_')}_exam_prep.pdf"
    build_pdf(
        sections,
        output_path,
        course_name=course.course_name,
        chapter_titles=[u.name for u in course.units],
    )
    print(f"\nDone. Wrote: {output_path}")


if __name__ == "__main__":
    main()
