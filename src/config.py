"""Load a course config YAML into simple objects."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml
from pypdf import PdfReader

from . import pdf_utils
from .prompts import (BOOK_PROMPT_VERSION, PROMPT_VERSION, TRANSCRIPT_ONLY_PROMPT_VERSION, TRANSCRIPT_PROMPT_VERSION,
                      TRANSCRIPT_SHARED_PROMPT_VERSION)

DEFAULT_MODEL = "claude-haiku-5-5"

# Folder-name spellings we'll recognize when auto-discovering a course from
# a plain directory (case-insensitive), so no YAML is needed for the common
# case of "one folder with a Lectures subfolder and an Exercises subfolder".
_LECTURE_DIR_NAMES = {"lectures", "lecture", "slides"}
_EXERCISE_DIR_NAMES = {"exercises", "exercise", "exrecises", "tutorials", "tutorial"}
# Past-exam folders, used only when --with-tests is passed.
_EXAM_DIR_NAMES = {
    "tests", "test", "exams", "exam", "past exams", "previous exams",
    "מבחנים", "מבחנים קודמים", "בחינות",
}
# Lecture-recording transcripts (.txt), looked for anywhere under the course.
_TRANSCRIPT_DIR_NAMES = {"transcript", "transcripts", "transcipts", "תמלול", "תמלולים"}


def _natural_key(s: str) -> list:
    """Sort key so 'L2' sorts before 'L10' instead of after."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def fingerprint(paths, *tags: str) -> str:
    """sha256 of the tags, then each path's name and mtime (when it exists)."""
    h = hashlib.sha256()
    for t in tags:
        h.update(t.encode("utf-8"))
    for p in paths:
        h.update(p.name.encode("utf-8"))
        if p.exists():
            h.update(str(p.stat().st_mtime_ns).encode("utf-8"))
    return h.hexdigest()


def _clean_unit_name(stem: str) -> str:
    name = re.sub(r"[_]+", " ", stem).strip()
    return re.sub(r"\s+", " ", name)


@dataclass
class Unit:
    name: str
    lecture_files: List[str] = field(default_factory=list)
    exercise_files: List[str] = field(default_factory=list)
    # .txt transcripts of the lecture recordings that cover these slides, in
    # course.transcripts_dir. Usually one; a recording named for a range
    # ("L1-2.txt") is attached to every deck in it.
    transcript_files: List[str] = field(default_factory=list)
    # Other decks' lecture PDFs that share one of those recordings.
    other_decks: List[str] = field(default_factory=list)

    def transcript_paths(self, course: "CourseConfig") -> List[Path]:
        return [course.transcripts_dir / f for f in self.transcript_files]

    def source_paths(self, course: "CourseConfig") -> List[Path]:
        paths = [course.lectures_dir / f for f in self.lecture_files]
        paths += [course.exercises_dir / f for f in self.exercise_files]
        return paths

    def cache_key(self, course: "CourseConfig") -> str:
        """Hash of model + file names + mtimes, so a model switch or edited/new files invalidate the cache."""
        # Optional tags are only folded in when set, so regular courses keep their old keys.
        tags = [PROMPT_VERSION, self.name, course.model]
        tags += [BOOK_PROMPT_VERSION] if course.book else []
        tags += [t for t in (course.focus, course.instructions) if t]
        paths = self.source_paths(course)
        if self.transcript_files:
            tags.append(TRANSCRIPT_PROMPT_VERSION)
            if not self.lecture_files:
                tags.append(TRANSCRIPT_ONLY_PROMPT_VERSION)
            if self.other_decks:
                tags.append(TRANSCRIPT_SHARED_PROMPT_VERSION)
                paths += [course.lectures_dir / f for f in self.other_decks]
            paths = self.transcript_paths(course) + paths
        return fingerprint(paths, *tags)[:16]


@dataclass
class CourseConfig:
    course_name: str
    root_dir: Path
    lectures_dir: Path
    exercises_dir: Path
    output_dir: Path
    units: List[Unit]
    model: str = DEFAULT_MODEL
    # Folder of past-exam PDFs. Only used when the run asks for test
    # questions (--with-tests); None when no such folder was found.
    exams_dir: Optional[Path] = None
    # Book mode (--book): units are textbook chapters, not slide decks.
    book: bool = False
    # Text of an assignment (--focus) the summary should go deep on.
    focus: str = ""
    # Extra prompt text from --instructions, added to every call's system prompt.
    instructions: str = ""
    # Folder of lecture transcripts, set by attach_transcripts().
    transcripts_dir: Optional[Path] = None

    def exam_paths(self) -> List[Path]:
        """Past-exam PDFs, natural-sorted. Same leading-underscore skip rule
        as lecture/exercise discovery. Empty if there's no exams folder."""
        if not self.exams_dir or not self.exams_dir.exists():
            return []
        return sorted(
            (p for p in self.exams_dir.glob("*.pdf") if not p.stem.startswith("_")),
            key=lambda p: _natural_key(p.stem),
        )


def load_course_config(yaml_path: Path) -> CourseConfig:
    yaml_path = Path(yaml_path)
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    root_dir = yaml_path.parent  # config files live in course_summarizer/courses/

    def resolve(p: Optional[str], default: str) -> Path:
        raw = p if p else default
        path = Path(raw)
        if not path.is_absolute():
            path = (root_dir / path).resolve()
        return path

    lectures_dir = resolve(data.get("lectures_dir"), f"../../{data['course_name']}/Lectures")
    exercises_dir = resolve(data.get("exercises_dir"), f"../../{data['course_name']}/Exercises")
    output_dir = resolve(data.get("output_dir"), f"../output/{data['course_name']}")

    # An explicit exams_dir wins; otherwise look for a conventionally-named
    # folder next to the lectures, and leave it unset if there isn't one.
    if data.get("exams_dir"):
        exams_dir = resolve(data["exams_dir"], "")
    else:
        exams_dir = _find_subdir(lectures_dir.parent, _EXAM_DIR_NAMES) if lectures_dir.parent.exists() else None

    units = [
        Unit(
            name=u["name"],
            lecture_files=u.get("lecture_files", []) or [],
            exercise_files=u.get("exercise_files", []) or [],
        )
        for u in data.get("units", [])
    ]

    if not units:
        raise ValueError(
            f"No units defined in {yaml_path}. Add at least one entry under 'units:'."
        )

    return CourseConfig(
        course_name=data["course_name"],
        root_dir=root_dir,
        lectures_dir=lectures_dir,
        exercises_dir=exercises_dir,
        output_dir=output_dir,
        units=units,
        model=data.get("model", DEFAULT_MODEL),
        exams_dir=exams_dir,
    )


def _find_subdir(course_dir: Path, candidate_names: set) -> Optional[Path]:
    for child in sorted(course_dir.iterdir()):
        if child.is_dir() and child.name.lower() in candidate_names:
            return child
    return None


def discover_course_config(
    course_dir: Path,
    output_root: Optional[Path] = None,
) -> CourseConfig:
    """Build a CourseConfig straight from a course folder, no YAML needed.

    Expects `course_dir` to contain a Lectures-like subfolder and/or an
    Exercises-like subfolder (name matching is case-insensitive and tolerant
    of the "Exrecises" typo). Every PDF found becomes its own unit, named
    after the file (underscores -> spaces), in natural filename order so
    e.g. "L2" sorts before "L10".
    """
    course_dir = Path(course_dir).resolve()
    if not course_dir.exists() or not course_dir.is_dir():
        raise FileNotFoundError(f"Course folder not found: {course_dir}")

    course_name = course_dir.name
    lectures_dir = _find_subdir(course_dir, _LECTURE_DIR_NAMES) or (course_dir / "Lectures")
    exercises_dir = _find_subdir(course_dir, _EXERCISE_DIR_NAMES) or (course_dir / "Exercises")
    exams_dir = _find_subdir(course_dir, _EXAM_DIR_NAMES)
    output_dir = Path(output_root).resolve() / course_name if output_root else (
        Path(__file__).parent.parent / "output" / course_name
    )

    def pdfs_in(d: Path) -> List[Path]:
        if not d.exists():
            return []
        # A leading underscore excludes a file (e.g. rename a duplicate/merged
        # PDF to "_exercises.pdf" to skip it without deleting it).
        return sorted(
            (p for p in d.glob("*.pdf") if not p.stem.startswith("_")),
            key=lambda p: _natural_key(p.stem),
        )

    units = [
        Unit(name=_clean_unit_name(p.stem), lecture_files=[p.name])
        for p in pdfs_in(lectures_dir)
    ]
    units += [
        Unit(name=f"{_clean_unit_name(p.stem)} (Exercise)", exercise_files=[p.name])
        for p in pdfs_in(exercises_dir)
    ]

    if not units:
        raise ValueError(
            f"No PDFs found under {lectures_dir} or {exercises_dir}. "
            f"Expected a course folder with a Lectures and/or Exercises subfolder."
        )

    return CourseConfig(
        course_name=course_name,
        root_dir=course_dir,
        lectures_dir=lectures_dir,
        exercises_dir=exercises_dir,
        output_dir=output_dir,
        units=units,
        exams_dir=exams_dir,
    )


def _lecture_keys(stem: str) -> list:
    """"L1 - Intro" and "L1" have keys [1], "L1-3" or "L1-L3" [1, 2, 3], "L1,3" [1, 3];
    a name with no leading number is its own key."""
    m = re.match(r"\s*L?\s*(\d+(?:[-,]L?\d+)*)", stem, re.I)
    if not m:
        return [stem.strip().lower()]
    keys = []
    for piece in m[1].split(","):
        ends = [int(n.lstrip("Ll")) for n in piece.split("-")]
        keys += range(ends[0], ends[-1] + 1)
    return keys


def attach_transcripts(course: CourseConfig) -> int:
    """Pairs each .txt in the course's transcripts folder with the lecture units
    of its lecture keys (see _lecture_keys); a transcript with no lecture PDF
    becomes a lecture unit of its own. Returns how many were found."""
    root = course.lectures_dir.parent
    found = sorted(d for d in root.rglob("*") if d.is_dir() and d.name.lower() in _TRANSCRIPT_DIR_NAMES) \
        if root.exists() else []
    if not found:
        return 0
    course.transcripts_dir = found[0]
    texts = sorted((p for p in course.transcripts_dir.glob("*.txt") if not p.stem.startswith("_")),
                   key=lambda p: _natural_key(p.stem))
    by_key = {_lecture_keys(Path(u.lecture_files[0]).stem)[0]: u for u in course.units if u.lecture_files}
    added = []
    for t in texts:
        units = [by_key[k] for k in _lecture_keys(t.stem) if k in by_key]
        for u in units:
            u.transcript_files.append(t.name)
            u.other_decks += [f for o in units if o is not u for f in o.lecture_files if f not in u.other_decks]
        if not units:
            print(f"  transcript {t.name} matches no lecture PDF - summarizing it on its own")
            added.append(Unit(name=_clean_unit_name(t.stem), transcript_files=[t.name]))
    if added:
        # ponytail: re-sorts lectures by name, so a YAML's hand-picked unit
        # order is lost when it gets a transcript-only lecture.
        lectures = [u for u in course.units if u.lecture_files] + added
        course.units = sorted(lectures, key=lambda u: _natural_key(u.name)) + \
            [u for u in course.units if not u.lecture_files]
    return len(texts)


def ensure_course_folders(course_dir: Path) -> List[Path]:
    """Creates Lectures and Recordings/Transcripts under course_dir unless a
    folder the pipeline recognizes (any spelling above) is already there.
    Returns the folders it created."""
    created = []
    if not _find_subdir(course_dir, _LECTURE_DIR_NAMES):
        created.append(course_dir / "Lectures")
    if not any(d.is_dir() and d.name.lower() in _TRANSCRIPT_DIR_NAMES for d in course_dir.rglob("*")):
        # Beside recordings that are already there (where the transcriber puts its
        # .txt), not in a second Recordings tree.
        recording = next((p for p in course_dir.rglob("*") if p.suffix.lower() in (".mp4", ".m4a", ".mp3")), None)
        created.append((recording.parent if recording else course_dir / "Recordings") / "Transcripts")
    for d in created:
        d.mkdir(parents=True)
    return created


def parse_chapters(spec: str) -> List[int]:
    """'1-5,7' -> [1, 2, 3, 4, 5, 7]."""
    chapters = []
    for part in spec.replace(" ", "").split(","):
        first, _, last = part.partition("-")
        if not first.isdigit() or (last and not last.isdigit()):
            raise ValueError(f"Bad --chapters value {spec!r}, expected e.g. 1-5 or 1,3,5")
        chapters += range(int(first), int(last or first) + 1)
    return sorted(set(chapters))


def discover_book_config(
    book_path: Path,
    spec: str,
    focus_path: Optional[Path] = None,
    output_root: Optional[Path] = None,
) -> CourseConfig:
    """Build a CourseConfig whose units are the chapters in `spec` (e.g. "1-5,7") of a textbook
    PDF, found from its bookmarks. Each chapter is cut out once into the
    output cache and reused after that."""
    book_path = Path(book_path).resolve()
    if not book_path.is_file():
        raise FileNotFoundError(f"Book not found: {book_path}")
    chapters = parse_chapters(spec)
    reader = PdfReader(str(book_path))
    ranges = pdf_utils.chapter_ranges(reader)
    if not ranges:
        raise ValueError(f"{book_path.name} has no chapter bookmarks to find chapters by")
    unknown = [c for c in chapters if not 1 <= c <= len(ranges)]
    if unknown:
        raise ValueError(f"{book_path.name} has chapters 1-{len(ranges)}, no chapter {unknown}")

    output_dir = Path(output_root).resolve() / book_path.stem if output_root else (
        Path(__file__).parent.parent / "output" / book_path.stem
    )
    chapters_dir = output_dir / "cache" / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    units = []
    for c in chapters:
        title, first, last = ranges[c - 1]
        out = chapters_dir / f"ch{c:02d}_p{first}-{last}.pdf"
        # Written once only: the cache key uses the file's mtime.
        if not out.exists():
            pdf_utils.extract_pages(reader, first, last, out)
        units.append(Unit(name=f"Chapter {c}: {title}", lecture_files=[out.name]))

    return CourseConfig(
        course_name=f"{book_path.stem} ch{spec.replace(' ', '')}",
        root_dir=book_path.parent,
        lectures_dir=chapters_dir,
        exercises_dir=chapters_dir,
        output_dir=output_dir,
        units=units,
        book=True,
        focus=pdf_utils.extract_text(Path(focus_path)) if focus_path else "",
    )
