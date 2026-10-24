import sys
from pathlib import Path
from types import SimpleNamespace

from pypdf import PdfReader, PdfWriter

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import pdf_utils, pipeline
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


def test_process_unit_runs_one_chat_per_part_with_continuous_numbering(tmp_path, monkeypatch):
    (tmp_path / "lec").mkdir()
    make_pdf(tmp_path / "lec" / "L1.pdf", 4)
    course = SimpleNamespace(
        course_name="C", model="m", output_dir=tmp_path / "out",
        lectures_dir=tmp_path / "lec", exercises_dir=tmp_path / "ex", book=False, focus="",
    )
    unit = Unit(name="Unit 1", lecture_files=["L1.pdf"])
    monkeypatch.setattr(Unit, "cache_key", lambda self, c: "k")

    chats, uploads = [], []
    monkeypatch.setattr(pipeline.claude_client, "upload_pdf", lambda client, p: uploads.append(p.name) or p.name)
    monkeypatch.setattr(pipeline.claude_client, "start_chat", lambda *a, **kw: chats.append([]) or chats[-1])

    def fake_send(chat, parts):
        chat.append(parts)
        if parts[0].startswith("Here is the full-course"):
            return "1. Alpha\n2. Beta" if len(chats) == 1 else "1. Gamma\n2. Delta"
        if parts[0] == pipeline.prompts.GLOSSARY_PROMPT:
            return f"- **Term{len(chats)}** - x"
        return parts[0][-40:]

    monkeypatch.setattr(pipeline.claude_client, "send", fake_send)

    markdown = pipeline.process_unit(None, course, unit, digest="d", use_cache=False)

    assert uploads == ["L1_p1-2.pdf", "L1_p3-4.pdf"]
    assert len(chats) == 2
    assert "part 1/2" in chats[0][0][0] and chats[0][0][1] == "L1_p1-2.pdf"
    assert "- Topic 3: Gamma\n- Topic 4: Delta" in chats[1][2][0]
    assert "1. Alpha\n2. Beta\n3. Gamma\n4. Delta" in markdown
    assert "- **Term1** - x\n- **Term2** - x" in markdown


def test_book_unit_is_one_chat_of_three_calls(tmp_path, monkeypatch):
    (tmp_path / "lec").mkdir()
    make_pdf(tmp_path / "lec" / "ch01.pdf", 4)
    course = SimpleNamespace(
        course_name="C", model="m", output_dir=tmp_path / "out",
        lectures_dir=tmp_path / "lec", exercises_dir=tmp_path / "ex", book=True, focus="",
    )
    monkeypatch.setattr(Unit, "cache_key", lambda self, c: "k")
    sends = []
    monkeypatch.setattr(pipeline.claude_client, "upload_pdf", lambda client, p: p.name)
    monkeypatch.setattr(pipeline.claude_client, "start_chat", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline.claude_client, "send",
                        lambda chat, parts: sends.append(parts[0]) or ("1. A\n2. B\n3. C\n4. D\n5. E" if len(sends) == 1 else "x"))

    pipeline.process_unit(None, course, Unit(name="Ch 1", lecture_files=["ch01.pdf"]), digest="", use_cache=False)

    assert len(sends) == 3  # agenda, all topics, glossary
    assert sends[0].endswith(pipeline.prompts.BOOK_AGENDA_RULE)
    assert "- Topic 5: E" in sends[1] and "4-5 pages" in sends[1]
