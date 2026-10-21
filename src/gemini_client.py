"""Thin wrapper around the google-genai SDK: client init, native PDF upload
(with wait-for-ACTIVE), and chat-session creation with a fixed system prompt."""
from __future__ import annotations

import os
import random
import time
from pathlib import Path
from typing import List

from google import genai
from google.genai import types

DEFAULT_MODEL = "gemini-3-flash-preview"

# Hard ceiling per HTTP request. Without this, a stalled connection or a
# server-side hang blocks forever with zero CPU/network signal - which is
# exactly what happened during testing (a call sat "running" for 90+
# minutes with no progress). Better to fail loudly and let a retry happen.
REQUEST_TIMEOUT_MS = 10 * 60 * 1000

# Retry budget for one chat turn. A failed call is never recorded in the
# chat's history, so re-sending the same parts is safe.
MAX_ATTEMPTS = 4
# Bad request / bad key / no access / unknown model - retrying can't help.
NON_RETRYABLE_STATUS = {400, 401, 403, 404}

# A reply that hits max_output_tokens stops mid-sentence. It stays in the
# chat history, so asking the model to continue picks up right where it cut.
MAX_CONTINUATIONS = 3
CONTINUE_PROMPT = (
    "Your previous reply was cut off by the output length limit. Continue it "
    "from the exact character where it stopped - do not repeat anything already "
    "written, do not add any introduction, and keep the same formatting rules."
)


def make_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Add it to your .env file "
            "(see .env.example) or export it in your shell."
        )
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
    )


def upload_pdf(client: genai.Client, pdf_path: Path, poll_seconds: float = 2.0, timeout_seconds: float = 300.0):
    """Uploads a PDF via the Files API and blocks until Gemini finishes
    processing it (state ACTIVE), so the very next generate call can use it."""
    uploaded = client.files.upload(file=str(pdf_path))

    waited = 0.0
    file_info = client.files.get(name=uploaded.name)
    while file_info.state.name == "PROCESSING":
        if waited >= timeout_seconds:
            raise TimeoutError(f"Timed out waiting for {pdf_path.name} to finish processing on Gemini.")
        time.sleep(poll_seconds)
        waited += poll_seconds
        file_info = client.files.get(name=uploaded.name)

    if file_info.state.name != "ACTIVE":
        raise RuntimeError(f"Gemini file upload for {pdf_path.name} ended in state {file_info.state.name}")

    return file_info


def start_chat(client: genai.Client, model: str, system_instruction: str, max_output_tokens: int = 8192):
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        max_output_tokens=max_output_tokens,
        temperature=0.3,
    )
    return client.chats.create(model=model, config=config)


def _is_permanent(exc: Exception) -> bool:
    """A 429 is usually a per-minute rate limit that clears in seconds, but a
    used-up daily quota is also a 429 and won't recover within the run. The
    error body names the exhausted quota, e.g.
    "GenerateRequestsPerDayPerProjectPerModel-FreeTier"."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 429:
        return "PerDay" in str(exc)
    return code in NON_RETRYABLE_STATUS


def _send_with_retry(chat, parts: List):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return chat.send_message(parts)
        except Exception as exc:
            if _is_permanent(exc) or attempt == MAX_ATTEMPTS:
                raise
            delay = 5 * 2 ** attempt + random.uniform(0, 1)
            print(f"  [retry] Gemini call failed ({type(exc).__name__}: {str(exc)[:200]}); "
                  f"retry {attempt}/{MAX_ATTEMPTS - 1} in {delay:.0f}s")
            time.sleep(delay)


def _finish_reason(response):
    return response.candidates[0].finish_reason if response.candidates else None


def send(chat, parts: List) -> str:
    response = _send_with_retry(chat, parts)
    text = response.text or ""
    for _ in range(MAX_CONTINUATIONS):
        if _finish_reason(response) != types.FinishReason.MAX_TOKENS:
            break
        print("  [continue] reply hit the output limit - asking Gemini to continue it")
        response = _send_with_retry(chat, [CONTINUE_PROMPT])
        # ponytail: plain concatenation - a cut mid-word can leave a stray
        # space or doubled word at the seam; dedupe the overlap if it shows up.
        text += response.text or ""

    if _finish_reason(response) == types.FinishReason.MAX_TOKENS:
        raise RuntimeError(
            f"Gemini reply still cut off after {MAX_CONTINUATIONS} continuations - "
            f"raise max_output_tokens or lower SECTION_BATCH_SIZE in src/pipeline.py"
        )
    if not text:
        raise RuntimeError(f"Empty response from Gemini (finish_reason={_finish_reason(response) or '?'})")
    return text
