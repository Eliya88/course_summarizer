"""Thin wrapper around the Anthropic SDK: client init, PDF loading as
document blocks, and a small chat object that keeps the conversation history
(the Messages API is stateless)."""
from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import List

import anthropic

# Hard ceiling per HTTP request, so a stalled connection fails loudly
# instead of hanging forever.
REQUEST_TIMEOUT_S = 10 * 60
# The SDK retries connection errors, 408, 409, 429 and 5xx with backoff.
# 400/401/403/404 and an empty credit balance (a 400) fail fast.
MAX_RETRIES = 4

# A reply that hits max_tokens stops mid-sentence. It stays in the chat
# history, so asking the model to continue picks up right where it cut.
MAX_CONTINUATIONS = 3
CONTINUE_PROMPT = (
    "Your previous reply was cut off by the output length limit. Continue it "
    "from the exact character where it stopped - do not repeat anything already "
    "written, do not add any introduction, and keep the same formatting rules."
)


def make_client() -> anthropic.Anthropic:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to your .env file "
            "(see .env.example) or export it in your shell."
        )
    return anthropic.Anthropic(timeout=REQUEST_TIMEOUT_S, max_retries=MAX_RETRIES)


def upload_pdf(client, pdf_path: Path) -> dict:
    """Returns the PDF as an inline document block. Claude reads it natively
    (text and page images), like the Gemini file upload did."""
    # ponytail: inline base64 - a request is capped at 32 MB, so a large
    # --with-tests exam archive can hit it; move to the Files API if it does.
    data = base64.standard_b64encode(pdf_path.read_bytes()).decode("ascii")
    return {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": data},
        "title": pdf_path.name,
    }


class Chat:
    def __init__(self, client, model: str, system: str, max_tokens: int):
        self.client, self.model, self.system, self.max_tokens = client, model, system, max_tokens
        self.messages: List[dict] = []

    def send_message(self, parts: List):
        # Documents go before the text that refers to them.
        docs = [p for p in parts if not isinstance(p, str)]
        texts = [{"type": "text", "text": p} for p in parts if isinstance(p, str)]
        self.messages.append({"role": "user", "content": docs + texts})
        try:
            # Streaming keeps a long reply under the HTTP timeout. Automatic
            # caching makes every later turn read the digest + PDFs from cache.
            with self.client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=self.system,
                messages=self.messages,
                cache_control={"type": "ephemeral"},
            ) as stream:
                response = stream.get_final_message()
        except Exception:
            self.messages.pop()  # keep history valid if the caller retries
            raise
        # Full content, not just the text: thinking blocks must be sent back.
        self.messages.append({"role": "assistant", "content": response.content})
        return response


def start_chat(client, model: str, system_instruction: str, max_output_tokens: int = 8192) -> Chat:
    return Chat(client, model, system_instruction, max_output_tokens)


def _text(response) -> str:
    return "".join(b.text for b in response.content if b.type == "text")


def send(chat, parts: List) -> str:
    response = chat.send_message(parts)
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the request: {response.stop_details}")
    text = _text(response)
    for _ in range(MAX_CONTINUATIONS):
        if response.stop_reason != "max_tokens":
            break
        print("  [continue] reply hit the output limit - asking Claude to continue it")
        response = chat.send_message([CONTINUE_PROMPT])
        # ponytail: plain concatenation - a cut mid-word can leave a stray
        # space or doubled word at the seam; dedupe the overlap if it shows up.
        text += _text(response)

    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            f"Claude reply still cut off after {MAX_CONTINUATIONS} continuations - "
            f"raise max_output_tokens or lower SECTION_BATCH_SIZE in src/pipeline.py"
        )
    if not text:
        raise RuntimeError(f"Empty response from Claude (stop_reason={response.stop_reason})")
    return text
