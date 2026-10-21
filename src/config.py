"""Load a course config YAML into simple objects."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

from .prompts import PROMPT_VERSION

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


def _natural_key(s: str) -> list:
    """Sort key so 'L2' sorts before 'L10' instead of after."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _clean_unit_name(stem: str) -> str:
    name = re.sub(r"[_]+", " ", stem).strip()
    return re.sub(r"\s+", " ", name)


@dataclass
class Unit:
    name: str
    lecture_files: List[str] = field(default_factory=list)
    exercise_files: List[str] = field(default_factory=list)

    def source_paths(self, course: "CourseConfig") -> List[Path]:
        paths = [course.lectures_dir / f for f in self.lecture_files]
        paths += [course.exercises_dir / f for f in self.exercise_files]
        return paths

    def cache_key(self, course: "CourseConfig") -> str:
        """Hash of model + file names + mtimes, so a model switch or edited/new files invalidate the cache."""
        h = hashlib.sha256()
        h.update(PROMPT_VERSION.encode("utf-8"))
        h.update(self.name.encode("utf-8"))
        h.update(course.model.encode("utf-8"))
        for p in self.source_paths(course):
            h.update(p.name.encode("utf-8"))
            if p.exists():
                h.update(str(p.stat().st_mtime_ns).encode("utf-8"))
        return h.hexdigest()[:16]


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

    root_dir = yaml_path.parent  # config files live in exam_prep_summarizer/courses/

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
    model: str = DEFAULT_MODEL,
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
        model=model,
        exams_dir=exams_dir,
    )
