"""Orchestrates the whole run: for each configured unit, cut its native
PDF(s) into page ranges, upload each range and walk it through its own
multi-turn chat (agenda -> one detailed pass per section -> glossary), and
assemble one master Markdown document.

The section-by-section loop is the fix for the original problem: asking
Claude for one giant summary in a single turn makes it compress/skip
material. Asking for one dedicated, generously-budgeted response per section
- while keeping the whole unit in one chat session for continuity - gets
exhaustive coverage without needing several manual chat sessions.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import List, Optional

from . import claude_client, pdf_utils, prompts
from .config import CourseConfig, Unit
from .course_context import build_course_context

# How many agenda topics go into one Claude call. Batching cuts wall-clock
# time and API call count roughly by this factor versus one call per topic,
# at the cost of a larger per-call output budget.
SECTION_BATCH_SIZE = 4
# A slide deck part gets at most one agenda topic per this many slides.
SLIDES_PER_TOPIC = 3
# A slide-less (transcript-only) lecture gets one topic per this many words.
WORDS_PER_TOPIC = 1500
SECTION_MAX_OUTPUT_TOKENS = 24000

# Each source PDF is cut into this many page ranges, and each range gets its
# own chat, so every call covers less material and a small model like
# Flash-Lite skims less.
# ponytail: plain page-count split - a topic that straddles the cut can be
# covered in both parts; split on the deck's section-title slides if that shows.
PARTS_PER_FILE = 2


def _parse_agenda(agenda_text: str) -> List[str]:
    titles = []
    for line in agenda_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # strip leading list markers like "1.", "1)", "-", "*"
        cleaned = re.sub(r"^(\d+[\.\)]|[-*])\s*", "", line).strip()
        cleaned = re.sub(r"\*+", "", cleaned).strip()
        if cleaned:
            titles.append(cleaned)
    return titles if len(titles) >= 2 else ["Full deck"]


def _unit_cache_path(course: CourseConfig, unit: Unit) -> Path:
    return course.output_dir / "cache" / f"unit_{unit.cache_key(course)}.json"


def _exam_fingerprint(course: CourseConfig) -> str:
    """Identity of the past-exam material + the prompt that consumes it, so
    swapping in a new exam paper regenerates the practice pages."""
    h = hashlib.sha256()
    h.update(prompts.EXAM_PROMPT_VERSION.encode("utf-8"))
    for p in course.exam_paths():
        h.update(p.name.encode("utf-8"))
        h.update(str(p.stat().st_mtime_ns).encode("utf-8"))
    return h.hexdigest()[:12]


def _exam_cache_path(course: CourseConfig, unit: Unit, exam_fp: str) -> Path:
    """Exam pages are cached apart from the chapter summary they attach to,
    so turning --with-tests on later doesn't throw away the summaries."""
    return course.output_dir / "cache" / f"exam_{unit.cache_key(course)}_{exam_fp}.json"


def generate_exam_block(client, course: CourseConfig, unit: Unit, unit_markdown: str,
                        exam_files: List, exam_fp: str, use_cache: bool = True) -> Optional[str]:
    """Asks for one past-exam question covering this unit, plus a worked
    solution. Returns the Markdown block, or None when the exams contain
    nothing relevant to this unit.

    Deliberately a fresh one-shot chat rather than a turn inside the unit's
    summarising chat: that way it works identically whether the summary was
    just generated or loaded from cache, and the exam PDFs are sent exactly
    once per unit instead of riding along in every turn of a long session.
    """
    cache_path = _exam_cache_path(course, unit, exam_fp)
    if use_cache and cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))["markdown"]
        print(f"  Using cached exam question for '{unit.name}'")
        return cached or None

    print(f"  Looking for a past-exam question on '{unit.name}'...")
    system_instruction = _system_instruction(course)
    chat = claude_client.start_chat(
        client, course.model, system_instruction, max_output_tokens=SECTION_MAX_OUTPUT_TOKENS
    )
    prompt = prompts.EXAM_PROMPT.format(
        unit_name=unit.name,
        unit_summary=unit_markdown,
        no_question_marker=prompts.EXAM_NO_QUESTION_MARKER,
    )
    answer = claude_client.send(chat, [prompt, *exam_files]).strip()

    if prompts.EXAM_NO_QUESTION_MARKER in answer:
        print(f"  No past-exam question matches '{unit.name}' - skipping its practice page")
        block = ""
    else:
        block = "## שאלה ממבחנים קודמים\n\n" + answer

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"markdown": block}, ensure_ascii=False, indent=2), encoding="utf-8")
    return block or None


def _system_instruction(course: CourseConfig, unit: Optional[Unit] = None) -> str:
    """`unit` adds its transcript note; the exam call leaves it out, as it sends no transcript."""
    extra = prompts.BOOK_NOTE if course.book else ""
    if course.focus:
        extra += prompts.FOCUS_NOTE.format(focus=course.focus)
    if unit and unit.transcript_file:
        extra += prompts.TRANSCRIPT_NOTE if unit.lecture_files else prompts.TRANSCRIPT_ONLY_NOTE
    if course.instructions:
        extra += prompts.INSTRUCTIONS_NOTE.format(instructions=course.instructions)
    return prompts.SYSTEM_INSTRUCTION.format(course_name=course.course_name, extra=extra)


def _group_parts(per_file: List[List[Path]]) -> List[List[Path]]:
    """[[a1, a2], [b1]] -> [[a1, b1], [a2]]: part k holds each file's k-th chunk."""
    n_parts = max((len(chunks) for chunks in per_file), default=0)
    return [[chunks[k] for chunks in per_file if k < len(chunks)] for k in range(n_parts)]


_GLOSSARY_TERM_RE = re.compile(r"^\s*[-*]\s*\*\*(.+?)\*\*", re.M)
# Cap on the later slides' text an earlier transcript part is shown.
LATER_SLIDES_MAX_CHARS = 6000


def _merge_glossaries(glossaries: List[str]) -> str:
    """Joins the per-part glossaries, keeping only the first entry for a term
    that shows up in more than one part."""
    seen, lines = set(), []
    for line in "\n".join(glossaries).splitlines():
        match = _GLOSSARY_TERM_RE.match(line)
        # "Stasis (מרידה פנימית)" is the same term as "Stasis".
        term = match.group(1).split("(")[0].strip().lower() if match else None
        if term in seen:
            continue
        if term:
            seen.add(term)
        lines.append(line)
    return "\n".join(lines)


_IMG_MARKER_RE = re.compile(r"^\s*@@IMG@@\s*(\d+)\s+(\d+)\s*$", re.M)


def _resolve_images(body: str, part_paths: List[Path], img_dir: Path) -> str:
    """Turns each "@@IMG@@ <deck> <page>" the model wrote into
    "@@IMG@@ <image path>", extracting that page's figure from the chunk PDF
    the model actually saw. Markers with no usable image are dropped."""
    def resolve(m: re.Match) -> str:
        deck, page = int(m.group(1)), int(m.group(2))
        if not 1 <= deck <= len(part_paths):
            return ""
        img = pdf_utils.extract_main_image(part_paths[deck - 1], page, img_dir)
        return f"@@IMG@@ {img.resolve()}" if img else ""
    return _IMG_MARKER_RE.sub(resolve, body)


def _summarize_part(client, course: CourseConfig, system_instruction: str, digest: str, label: str,
                    uploaded_files: List, first_index: int, part_paths: List[Path], transcript_words: int = 0,
                    covered_titles: List[str] = (), later_slides: str = "", covered_terms: List[str] = ()):
    """One chat over one part of a unit: agenda -> outline -> sections ->
    glossary. Topic numbers start at `first_index` so they keep counting
    across parts. `part_paths` are the local chunk PDFs behind
    `uploaded_files`, used to pull out the slide images the model points at.
    `transcript_words` > 0 means a lecture transcript is attached too; with
    no `part_paths` it is the only source. `covered_titles` are the topics of
    the unit's earlier parts, and `later_slides` the text of its later parts'
    slides: a transcript part leaves both alone. `covered_terms` are the
    earlier parts' glossary terms. Returns (topic titles, section bodies,
    glossary)."""
    chat = claude_client.start_chat(
        client, course.model, system_instruction, max_output_tokens=SECTION_MAX_OUTPUT_TOKENS
    )

    agenda_prompt = prompts.AGENDA_PROMPT.format(digest=digest, unit_name=label)
    if course.book:
        agenda_prompt += prompts.BOOK_AGENDA_RULE
        if course.focus:
            agenda_prompt += prompts.FOCUS_AGENDA_RULE
    elif not part_paths:
        max_topics = max(3, math.ceil(transcript_words / WORDS_PER_TOPIC))
        agenda_prompt += prompts.TRANSCRIPT_ONLY_AGENDA_RULE.format(max_topics=max_topics)
    else:
        pages = sum(pdf_utils.page_count(p) for p in part_paths)
        agenda_prompt += prompts.SLIDE_AGENDA_RULE.format(max_topics=max(3, math.ceil(pages / SLIDES_PER_TOPIC)))
        if transcript_words:
            agenda_prompt += prompts.TRANSCRIPT_AGENDA_RULE
            if covered_titles:
                agenda_prompt += prompts.TRANSCRIPT_COVERED_RULE.format(
                    covered="\n".join(f"- {t}" for t in covered_titles))
            if later_slides:
                agenda_prompt += prompts.TRANSCRIPT_LATER_RULE.format(later=later_slides)
    section_titles = _parse_agenda(claude_client.send(chat, [agenda_prompt, *uploaded_files]))
    print(f"  Agenda: {len(section_titles)} section(s)")

    # A book's few broad topics are written in one call, so there are no
    # batches to keep apart and the boundary-planning call is skipped.
    batch_size = len(section_titles) if course.book else SECTION_BATCH_SIZE
    if not course.book:
        print("  Planning content boundaries...")
        claude_client.send(chat, [prompts.OUTLINE_PROMPT])

    if course.book:
        length_rule = prompts.BOOK_LENGTH_RULE
    elif transcript_words:
        length_rule = prompts.TRANSCRIPT_LENGTH_RULE if part_paths else prompts.TRANSCRIPT_ONLY_LENGTH_RULE
    else:
        length_rule = prompts.SLIDE_LENGTH_RULE
    numbered_titles = list(enumerate(section_titles, start=first_index))
    last_index = numbered_titles[-1][0]
    section_bodies = []
    for batch_start in range(0, len(numbered_titles), batch_size):
        batch = numbered_titles[batch_start:batch_start + batch_size]
        first_i, last_i = batch[0][0], batch[-1][0]
        print(f"  Writing topics {first_i}-{last_i}/{last_index}: "
              f"{', '.join(t for _, t in batch)}")
        topic_list = "\n".join(f"- Topic {i}: {t}" for i, t in batch)
        body = claude_client.send(chat, [prompts.SECTION_PROMPT.format(
            n=len(batch), topic_list=topic_list, length_rule=length_rule)])
        section_bodies.append(_resolve_images(body, part_paths, course.output_dir / "cache" / "images"))

    print("  Writing glossary...")
    glossary_prompt = prompts.GLOSSARY_PROMPT
    if covered_terms:
        glossary_prompt += prompts.GLOSSARY_COVERED_RULE.format(terms=", ".join(covered_terms))
    glossary_text = claude_client.send(chat, [glossary_prompt])
    return section_titles, section_bodies, glossary_text


def process_unit(client, course: CourseConfig, unit: Unit, digest: str, use_cache: bool = True) -> str:
    """Returns the assembled Markdown for one unit (agenda + sections + glossary)."""
    cache_path = _unit_cache_path(course, unit)
    if use_cache and cache_path.exists():
        print(f"[pipeline] Using cached result for unit '{unit.name}'")
        return json.loads(cache_path.read_text(encoding="utf-8"))["markdown"]

    print(f"[pipeline] Processing unit '{unit.name}'")
    source_paths = [p for p in unit.source_paths(course) if p.exists()]
    missing = [p for p in unit.source_paths(course) if not p.exists()]
    for m in missing:
        print(f"  [warn] configured file not found, skipping: {m}")
    transcript = unit.transcript_path(course)
    if not source_paths and not transcript:
        raise FileNotFoundError(f"No source PDFs found for unit '{unit.name}'")

    chunk_dir = course.output_dir / "cache" / "chunks"
    per_file = [pdf_utils.split_pdf(p, 1 if course.book else PARTS_PER_FILE, chunk_dir) for p in source_paths]
    # Part k = the k-th page range of every source file. A transcript-only
    # lecture is one part with no PDFs.
    parts = _group_parts(per_file) or [[]]
    # The whole transcript goes with every part: it has no timestamps to cut
    # it at the slides' page ranges, so the prompt picks the matching passages.
    transcript_doc = claude_client.text_document(transcript) if transcript else None
    transcript_words = len(transcript_doc["source"]["data"].split()) if transcript_doc else 0

    system_instruction = _system_instruction(course, unit)
    all_titles, section_bodies, glossaries = [], [], []
    for k, part in enumerate(parts, start=1):
        label = unit.name if len(parts) == 1 else f"{unit.name} (part {k}/{len(parts)})"
        names = [p.name for p in part] + ([transcript.name] if transcript else [])
        print(f"  Part {k}/{len(parts)}: uploading {', '.join(names)} to Claude...")
        uploaded_files = [claude_client.upload_pdf(client, p) for p in part]
        later_slides = ""
        if transcript_doc:
            uploaded_files.append(transcript_doc)
            later_slides = "\n\n".join(
                pdf_utils.extract_text(p) for later in parts[k:] for p in later)[:LATER_SLIDES_MAX_CHARS]
        titles, bodies, glossary = _summarize_part(
            client, course, system_instruction, digest, label, uploaded_files,
            first_index=len(all_titles) + 1, part_paths=part, transcript_words=transcript_words,
            covered_titles=all_titles, later_slides=later_slides.strip(),
            covered_terms=_GLOSSARY_TERM_RE.findall("\n".join(glossaries)),
        )
        all_titles += titles
        section_bodies += bodies
        glossaries.append(glossary)

    agenda_text = "\n".join(f"{i}. {t}" for i, t in enumerate(all_titles, start=1))
    markdown = "\n\n".join([
        f"# {unit.name}",
        "## נושאים:\n\n" + agenda_text,
        *section_bodies,
        "## מושגי מפתח\n\n" + _merge_glossaries(glossaries),
    ])

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"markdown": markdown}, ensure_ascii=False, indent=2), encoding="utf-8")

    return markdown


def run(course: CourseConfig, use_cache: bool = True, force_context: bool = False,
        with_tests: bool = False) -> List[str]:
    """Runs every unit and returns a list of top-level Markdown sections
    (cover page first, then one per unit) - kept separate so the PDF builder
    can insert a page break between each one.

    With `with_tests`, each unit also gets a past-exam practice question
    appended, provided the course has an exams folder to draw from."""
    client = claude_client.make_client()
    # A book's chapters are too long to resend as a digest on every call.
    digest = "" if course.book else build_course_context(course, force=force_context)

    exam_files, exam_fp = [], ""
    if with_tests:
        exam_paths = course.exam_paths()
        if not exam_paths:
            print("[pipeline] --with-tests was passed but no past-exam PDFs were found "
                  f"({course.exams_dir or 'no exams folder'}) - continuing without exam pages.")
        else:
            exam_fp = _exam_fingerprint(course)
            print(f"[pipeline] Uploading {len(exam_paths)} past-exam PDF(s) from {course.exams_dir.name}...")
            for p in exam_paths:
                print(f"  Uploading {p.name}...")
                exam_files.append(claude_client.upload_pdf(client, p))

    unit_markdowns = []
    for unit in course.units:
        markdown = process_unit(client, course, unit, digest, use_cache=use_cache)
        if exam_files:
            exam_block = generate_exam_block(
                client, course, unit, markdown, exam_files, exam_fp, use_cache=use_cache
            )
            if exam_block:
                markdown = markdown + "\n\n" + exam_block
        unit_markdowns.append(markdown)

    cover = f"# {course.course_name}\n## סיכום קורס\n\n" + "## תוכן עניינים\n\n" + "\n".join(
        f"@@TOC@@ {i}|{u.name}" for i, u in enumerate(course.units, start=1)
    )

    return [cover, *unit_markdowns]
