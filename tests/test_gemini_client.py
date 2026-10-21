import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.genai import types

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import gemini_client

DONE, CUT = types.FinishReason.STOP, types.FinishReason.MAX_TOKENS


def reply(text, finish=DONE):
    return SimpleNamespace(text=text, candidates=[SimpleNamespace(finish_reason=finish)])


class ApiError(Exception):
    def __init__(self, code, msg=""):
        super().__init__(msg)
        self.code = code


class FakeChat:
    """Plays back a scripted list of replies/exceptions, recording what was sent."""

    def __init__(self, *script):
        self.script, self.sent = list(script), []

    def send_message(self, parts):
        self.sent.append(parts)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(gemini_client.time, "sleep", lambda s: None)


def test_transient_errors_are_retried():
    chat = FakeChat(ApiError(503), ApiError(429, "GenerateRequestsPerMinute"), TimeoutError(), reply("ok"))
    assert gemini_client.send(chat, ["q"]) == "ok"
    assert chat.sent == [["q"]] * 4


def test_gives_up_after_max_attempts():
    chat = FakeChat(*[ApiError(503)] * gemini_client.MAX_ATTEMPTS)
    with pytest.raises(ApiError):
        gemini_client.send(chat, ["q"])


def test_permanent_errors_fail_fast():
    for exc in (ApiError(400), ApiError(429, "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")):
        chat = FakeChat(exc, reply("never reached"))
        with pytest.raises(ApiError):
            gemini_client.send(chat, ["q"])
        assert len(chat.sent) == 1


def test_cut_off_reply_is_continued():
    chat = FakeChat(reply("part one, ", CUT), reply("part two"))
    assert gemini_client.send(chat, ["q"]) == "part one, part two"
    assert chat.sent[1] == [gemini_client.CONTINUE_PROMPT]


def test_endless_cut_off_raises_instead_of_caching_partial_text():
    chat = FakeChat(*[reply("x", CUT)] * (gemini_client.MAX_CONTINUATIONS + 1))
    with pytest.raises(RuntimeError, match="cut off"):
        gemini_client.send(chat, ["q"])


def test_empty_reply_raises():
    with pytest.raises(RuntimeError, match="Empty"):
        gemini_client.send(FakeChat(reply(None)), ["q"])
