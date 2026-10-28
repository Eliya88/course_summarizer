import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import app

# Chapter 1 is cached, chapter 2 runs; their lines interleave.
RUN = """\
Course: LLM's  |  model: claude  |  units: 2
[2] [pipeline] Processing unit 'Attention'
[1] [pipeline] Using cached result for unit 'Intro'
[2]   Part 1/2: uploading a.pdf to Claude...
[1]   Looking for a past-exam question on 'Intro'...
[2]   Agenda: 6 section(s)
[2]   Planning content boundaries...
[1]   Using cached exam question for 'Intro'
[2]   Writing topics 1-4/6: x
[2]   Writing topics 5-6/6: y
[2]   Writing glossary...
[2]   Part 2/2: uploading a.pdf to Claude...
[2]   Planning content boundaries...
[2]   Writing topics 7-9/9: z
[2]   Writing glossary...
[2]   Using cached exam question for 'Attention'
Building the PDF...
""".splitlines()


def test_bar_only_moves_forward_and_stays_in_range():
    p = app.Progress(with_tests=True)
    values = []
    for line in RUN:
        p.feed(line)
        values.append(p.value())
    assert values == sorted(values)
    assert 0 <= values[0] and values[-1] <= 1
    assert p.label == "Building the PDF"


def test_cached_chapter_jumps_to_its_summary_end():
    p = app.Progress(with_tests=False)
    p.feed("Course: X  |  model: m  |  units: 4")
    p.feed("[3] [pipeline] Using cached result for unit 'A'")
    size = (1 - app.SETUP - app.PDF) / 4
    assert abs(p.value() - (app.SETUP + size)) < 1e-9
    assert p.label.startswith("Chapter 3 of 4")
