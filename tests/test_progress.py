import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import app

RUN = """\
Course: LLM's  |  model: claude  |  units: 2
[pipeline] Using cached result for unit 'Intro'
  Looking for a past-exam question on 'Intro'...
[pipeline] Processing unit 'Attention'
  Part 1/2: uploading a.pdf to Claude...
  Agenda: 6 section(s)
  Planning content boundaries...
  Writing topics 1-4/6: x
  Writing topics 5-6/6: y
  Writing glossary...
  Part 2/2: uploading a.pdf to Claude...
  Planning content boundaries...
  Writing topics 7-9/9: z
  Writing glossary...
  Using cached exam question for 'Attention'
Building the PDF...
""".splitlines()


def test_steps_only_move_forward_and_stay_in_range():
    p = app.Progress(with_tests=True)
    ends = []
    for line in RUN:
        p.feed(line)
        lo, hi = p.span
        assert 0 <= lo <= hi <= 1
        ends.append(hi)
    assert ends == sorted(ends)
    # Chapter 1 (cached) ends before its exam slice; the PDF step is last.
    assert p.span == (1 - app.PDF, 1.0)
    assert p.label == "Building the PDF"


def test_cached_chapter_jumps_to_its_summary_end():
    p = app.Progress(with_tests=False)
    p.feed("Course: X  |  model: m  |  units: 4")
    p.feed("[pipeline] Using cached result for unit 'A'")
    size = (1 - app.SETUP - app.PDF) / 4
    assert p.span == (app.SETUP + size, app.SETUP + size)
    assert p.label.startswith("Chapter 1 of 4")
