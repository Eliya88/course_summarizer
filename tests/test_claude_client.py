import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import claude_client


def reply(text, stop="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop, stop_details=None)


class FakeChat:
    """Plays back a scripted list of replies, recording what was sent."""

    def __init__(self, *script):
        self.script, self.sent = list(script), []

    def send_message(self, parts):
        self.sent.append(parts)
        return self.script.pop(0)


def test_cut_off_reply_is_continued():
    chat = FakeChat(reply("part one, ", "max_tokens"), reply("part two"))
    assert claude_client.send(chat, ["q"]) == "part one, part two"
    assert chat.sent[1] == [claude_client.CONTINUE_PROMPT]


def test_endless_cut_off_raises_instead_of_caching_partial_text():
    chat = FakeChat(*[reply("x", "max_tokens")] * (claude_client.MAX_CONTINUATIONS + 1))
    with pytest.raises(RuntimeError, match="cut off"):
        claude_client.send(chat, ["q"])


def test_empty_reply_and_refusal_raise():
    with pytest.raises(RuntimeError, match="Empty"):
        claude_client.send(FakeChat(reply("")), ["q"])
    with pytest.raises(RuntimeError, match="declined"):
        claude_client.send(FakeChat(reply("", "refusal")), ["q"])


class FakeStream:
    def __init__(self, calls, kwargs, result):
        calls.append({**kwargs, "messages": list(kwargs["messages"])})
        self.result = result

    def __enter__(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.result


def test_chat_keeps_history_and_puts_documents_first(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(tmp_path / "L1.pdf")
    doc = claude_client.upload_pdf(None, tmp_path / "L1.pdf")

    calls, results = [], [ConnectionError(), reply("a1"), reply("a2")]
    client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: FakeStream(calls, kw, results.pop(0))))
    chat = claude_client.start_chat(client, "m", "sys", max_output_tokens=10)

    with pytest.raises(ConnectionError):
        chat.send_message(["q1", doc])
    assert chat.messages == []  # failed turn is not left in the history

    chat.send_message(["q1", doc])
    chat.send_message(["q2"])
    first, second = calls[1]["messages"], calls[2]["messages"]
    assert [b["type"] for b in first[0]["content"]] == ["document", "text"]
    assert doc["source"]["media_type"] == "application/pdf"
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert calls[2]["system"] == "sys" and calls[2]["max_tokens"] == 10
