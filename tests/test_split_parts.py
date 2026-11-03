from types import SimpleNamespace

import pytest
from pypdf import PdfReader, PdfWriter

from src import config, pdf_utils, pipeline
from src.config import Unit


def make_pdf(path, pages):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    writer.write(path)
    return path


def test_split_pdf_into_page_ranges(tmp_path):
    chunks = pdf_utils.split_pdf(make_pdf(tmp_path / "deck.pdf", 5), 2, tmp_path / "out")
    assert [c.name for c in chunks] == ["deck_p1-3.pdf", "deck_p4-5.pdf"]
    assert [len(PdfReader(str(c)).pages) for c in chunks] == [3, 2]

    single = pdf_utils.split_pdf(make_pdf(tmp_path / "one.pdf", 1), 2, tmp_path / "out")
    assert [c.name for c in single] == ["one_p1-1.pdf"]


def test_group_parts_takes_kth_chunk_of_each_file():
    assert pipeline._group_parts([["a1", "a2"], ["b1"]]) == [["a1", "b1"], ["a2"]]


def test_merge_glossaries_drops_repeated_terms():
    merged = pipeline._merge_glossaries(["- **BERT** - a\n- **RNN** - b", "- **bert** - again\n- **LSTM** - c"])
    assert merged == "- **BERT** - a\n- **RNN** - b\n- **LSTM** - c"
    assert pipeline._merge_glossaries(["- **Stasis** - a", "- **Stasis (מרידה)** - b"]) == "- **Stasis** - a"


def test_process_unit_runs_one_chat_per_part_with_continuous_numbering(tmp_path, monkeypatch):
    (tmp_path / "lec").mkdir()
    make_pdf(tmp_path / "lec" / "L1.pdf", 4)
    course = SimpleNamespace(
        course_name="C", model="m", output_dir=tmp_path / "out",
        lectures_dir=tmp_path / "lec", exercises_dir=tmp_path / "ex", book=False, focus="", instructions="",
    )
    unit = Unit(name="Unit 1", lecture_files=["L1.pdf"])
    monkeypatch.setattr(Unit, "cache_key", lambda self, c: "k")

    chats, uploads = [], []
    monkeypatch.setattr(pipeline.claude_client, "pdf_document", lambda p: uploads.append(p.name) or p.name)
    monkeypatch.setattr(pipeline.claude_client, "Chat", lambda *a, **kw: chats.append([]) or chats[-1])

    def fake_send(chat, parts):
        chat.append(parts)
        if parts[0].startswith("Here is the full-course"):
            return "1. Alpha\n2. Beta" if len(chats) == 1 else "1. Gamma\n2. Delta"
        if parts[0].startswith(pipeline.prompts.GLOSSARY_PROMPT):
            return f"- **Term{len(chats)}** - x"
        return parts[0][-40:]

    monkeypatch.setattr(pipeline.claude_client, "send", fake_send)

    markdown = pipeline.process_unit(None, course, unit, digest="d", use_cache=False)

    assert uploads == ["L1_p1-2.pdf", "L1_p3-4.pdf"]
    assert len(chats) == 2
    assert "part 1/2" in chats[0][0][0] and chats[0][0][1] == "L1_p1-2.pdf"
    assert "Use at most 3 topics" in chats[0][0][0]  # 2 slides -> the floor of 3
    assert "- Topic 3: Gamma\n- Topic 4: Delta" in chats[1][2][0]
    assert "1. Alpha\n2. Beta\n3. Gamma\n4. Delta" in markdown
    assert "- **Term1** - x\n- **Term2** - x" in markdown


def test_rerun_after_a_failed_part_reuses_the_parts_before_it(tmp_path, monkeypatch):
    (tmp_path / "lec").mkdir()
    make_pdf(tmp_path / "lec" / "L1.pdf", 4)
    course = SimpleNamespace(
        course_name="C", model="m", output_dir=tmp_path / "out",
        lectures_dir=tmp_path / "lec", exercises_dir=tmp_path / "ex", book=False, focus="", instructions="",
    )
    unit = Unit(name="Unit 1", lecture_files=["L1.pdf"])
    monkeypatch.setattr(Unit, "cache_key", lambda self, c: "k")
    uploads = []
    monkeypatch.setattr(pipeline.claude_client, "pdf_document", lambda p: uploads.append(p.name) or p.name)
    monkeypatch.setattr(pipeline.claude_client, "Chat", lambda *a, **kw: None)
    fail = [True]

    def fake_send(chat, parts):
        if uploads[-1] == "L1_p3-4.pdf" and fail[0]:
            raise ConnectionError
        return "1. A\n2. B" if parts[0].startswith("Here is the full-course") else "x"

    monkeypatch.setattr(pipeline.claude_client, "send", fake_send)
    try:
        pipeline.process_unit(None, course, unit, digest="d")
    except ConnectionError:
        pass
    fail[0], uploads[:] = False, []
    pipeline.process_unit(None, course, unit, digest="d")
    assert uploads == ["L1_p3-4.pdf"]


def test_later_part_with_transcript_skips_earlier_topics(tmp_path, monkeypatch):
    (tmp_path / "lec").mkdir()
    make_pdf(tmp_path / "lec" / "L1.pdf", 4)
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "L1.txt").write_text("word " * 100, encoding="utf-8")
    course = SimpleNamespace(
        course_name="C", model="m", output_dir=tmp_path / "out", lectures_dir=tmp_path / "lec",
        exercises_dir=tmp_path / "ex", transcripts_dir=tmp_path / "t", book=False, focus="", instructions="",
    )
    monkeypatch.setattr(Unit, "cache_key", lambda self, c: "k")
    monkeypatch.setattr(pipeline.pdf_utils, "extract_text", lambda p: f"text of {p.name}")
    agendas, glossaries = [], []
    monkeypatch.setattr(pipeline.claude_client, "pdf_document", lambda p: p.name)
    monkeypatch.setattr(pipeline.claude_client, "Chat", lambda *a, **kw: None)

    def fake_send(chat, parts):
        if parts[0].startswith("Here is the full-course"):
            agendas.append(parts[0])
        if parts[0].startswith(pipeline.prompts.GLOSSARY_PROMPT):
            glossaries.append(parts[0])
            return "- **Helot** - x"
        return "1. Alpha\n2. Beta"

    monkeypatch.setattr(pipeline.claude_client, "send", fake_send)

    pipeline.process_unit(None, course, Unit(name="L1", lecture_files=["L1.pdf"], transcript_files=["L1.txt"]),
                          digest="d", use_cache=False)

    assert len(agendas) == 2
    assert agendas[0].endswith(pipeline.prompts.TRANSCRIPT_LATER_RULE.format(later="text of L1_p3-4.pdf"))
    assert "already covers" not in agendas[0]
    assert agendas[1].endswith(pipeline.prompts.TRANSCRIPT_COVERED_RULE.format(covered="- Alpha\n- Beta"))
    assert glossaries[0] == pipeline.prompts.GLOSSARY_PROMPT
    assert glossaries[1].endswith(pipeline.prompts.GLOSSARY_COVERED_RULE.format(terms="Helot"))


def test_slide_agenda_allows_one_topic_per_three_slides(tmp_path, monkeypatch):
    make_pdf(tmp_path / "L2.pdf", 24)
    course = SimpleNamespace(model="m", book=False, focus="", instructions="", output_dir=tmp_path / "out")
    sends = []
    monkeypatch.setattr(pipeline.claude_client, "Chat", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline.claude_client, "send", lambda chat, parts: sends.append(parts[0]) or "1. A\n2. B")

    pipeline._summarize_part(None, course, "sys", "d", "L2", [], 1, [tmp_path / "L2.pdf"])

    assert "Use at most 8 topics" in sends[0]


def test_book_unit_is_one_chat_of_three_calls(tmp_path, monkeypatch):
    (tmp_path / "lec").mkdir()
    make_pdf(tmp_path / "lec" / "ch01.pdf", 4)
    course = SimpleNamespace(
        course_name="C", model="m", output_dir=tmp_path / "out",
        lectures_dir=tmp_path / "lec", exercises_dir=tmp_path / "ex", book=True, focus="Implement BPE", instructions="",
    )
    monkeypatch.setattr(Unit, "cache_key", lambda self, c: "k")
    sends = []
    monkeypatch.setattr(pipeline.claude_client, "pdf_document", lambda p: p.name)
    monkeypatch.setattr(pipeline.claude_client, "Chat", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline.claude_client, "send",
                        lambda chat, parts: sends.append(parts[0]) or ("1. A\n2. B\n3. C\n4. D\n5. E" if len(sends) == 1 else "x"))

    pipeline.process_unit(None, course, Unit(name="Ch 1", lecture_files=["ch01.pdf"]), digest="", use_cache=False)

    assert len(sends) == 3  # agenda, all topics, glossary
    assert sends[0].endswith(pipeline.prompts.BOOK_AGENDA_RULE + pipeline.prompts.FOCUS_AGENDA_RULE)
    assert "- Topic 5: E" in sends[1] and "4-5 pages" in sends[1]


def test_transcripts_join_their_lecture_or_become_their_own(tmp_path):
    (tmp_path / "Lectures").mkdir()
    make_pdf(tmp_path / "Lectures" / "L1 - Intro.pdf", 1)
    make_pdf(tmp_path / "Lectures" / "L3.pdf", 1)
    (tmp_path / "Recordings" / "Transcript").mkdir(parents=True)
    for name in ("L1.txt", "L2.txt", "_L3.txt"):
        (tmp_path / "Recordings" / "Transcript" / name).write_text("x", encoding="utf-8")
    course = config.discover_course_config(tmp_path)

    assert config.attach_transcripts(course) == 2
    assert [(u.name, u.lecture_files, u.transcript_files) for u in course.units] == [
        ("L1 - Intro", ["L1 - Intro.pdf"], ["L1.txt"]),
        ("L2", [], ["L2.txt"]),
        ("L3", ["L3.pdf"], []),
    ]


def test_lecture_keys_read_ranges():
    assert config._lecture_keys("L1 - Intro") == [1]
    assert config._lecture_keys("L1-3") == config._lecture_keys("L1-L3") == [1, 2, 3]
    assert config._lecture_keys("L1,3") == [1, 3]
    assert config._lecture_keys("Intro") == ["intro"]


def test_a_recording_named_for_a_range_goes_to_each_deck(tmp_path, monkeypatch):
    (tmp_path / "Lectures").mkdir()
    for n in (1, 2, 3):
        make_pdf(tmp_path / "Lectures" / f"L{n}.pdf", 1)
    (tmp_path / "Transcripts").mkdir()
    for name in ("L1-2.txt", "L2-3.txt"):
        (tmp_path / "Transcripts" / name).write_text("x", encoding="utf-8")
    course = config.discover_course_config(tmp_path)

    config.attach_transcripts(course)
    assert [(u.transcript_files, u.other_decks) for u in course.units] == [
        (["L1-2.txt"], ["L2.pdf"]),
        (["L1-2.txt", "L2-3.txt"], ["L1.pdf", "L3.pdf"]),
        (["L2-3.txt"], ["L2.pdf"]),
    ]
    monkeypatch.setattr(pipeline.pdf_utils, "extract_text", lambda p: f"text of {p.name}")
    system = pipeline._system_instruction(course, course.units[0])
    assert pipeline.prompts.TRANSCRIPT_SHARED_NOTE.format(other="text of L2.pdf") in system


def test_transcript_only_unit_is_one_chat_over_the_text(tmp_path, monkeypatch):
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "L2.txt").write_text("word " * 4000, encoding="utf-8")
    course = SimpleNamespace(
        course_name="C", model="m", output_dir=tmp_path / "out", lectures_dir=tmp_path / "lec",
        exercises_dir=tmp_path / "ex", transcripts_dir=tmp_path / "t", book=False, focus="", instructions="",
    )
    monkeypatch.setattr(Unit, "cache_key", lambda self, c: "k")
    systems, sends = [], []
    monkeypatch.setattr(pipeline.claude_client, "pdf_document", lambda *a: pytest.fail("no PDF to upload"))
    monkeypatch.setattr(pipeline.claude_client, "Chat", lambda client, model, system, max_tokens: systems.append(system))
    monkeypatch.setattr(pipeline.claude_client, "send", lambda chat, parts: sends.append(parts) or "1. A\n2. B")

    pipeline.process_unit(None, course, Unit(name="L2", transcript_files=["L2.txt"]), digest="d", use_cache=False)

    assert len(systems) == 1 and pipeline.prompts.TRANSCRIPT_ONLY_NOTE in systems[0]
    agenda, doc = sends[0]
    assert doc["source"]["type"] == "text" and doc["title"] == "L2.txt"
    assert agenda.endswith(pipeline.prompts.TRANSCRIPT_ONLY_AGENDA_RULE.format(max_topics=3))  # 4000 words
    assert pipeline.prompts.TRANSCRIPT_ONLY_LENGTH_RULE in sends[2][0]


def test_run_overlaps_units_and_keeps_their_order(monkeypatch, capsys):
    import threading
    started = threading.Barrier(3, timeout=5)  # fails unless all 3 units run at once

    def fake_unit(client, course, unit, digest, use_cache=True):
        pipeline.log(f"start {unit.name}")
        started.wait()
        return unit.name

    monkeypatch.setattr(pipeline.claude_client, "make_client", lambda: None)
    monkeypatch.setattr(pipeline, "build_course_context", lambda course: "")
    monkeypatch.setattr(pipeline, "process_unit", fake_unit)
    course = SimpleNamespace(book=False, course_name="C", units=[Unit(name=n) for n in "ABC"])
    assert pipeline.run(course)[1:] == ["A", "B", "C"]
    lines = capsys.readouterr().out.splitlines()
    assert sorted(lines) == ["[1] start A", "[2] start B", "[3] start C"]


def test_ensure_course_folders_creates_only_what_is_missing(tmp_path):
    from src.config import ensure_course_folders
    assert ensure_course_folders(tmp_path) == [tmp_path / "Lectures", tmp_path / "Recordings" / "Transcripts"]
    assert ensure_course_folders(tmp_path) == []
    other = tmp_path / "other"
    (other / "slides").mkdir(parents=True)
    (other / "Audio" / "תמלול").mkdir(parents=True)
    assert ensure_course_folders(other) == []
