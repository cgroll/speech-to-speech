"""LLM backend: Gemini chat session with conversation history kept for the
lifetime of the app. Deliberately a single small `send()` function -- this
is the one place a future backend (e.g. Claude Code) would be swapped in.

Uses Vertex AI (project + Application Default Credentials) rather than a
plain Gemini-Developer-API key -- the pi-agent-gemini GCP project's org
policy blocks unbound API keys for the Generative Language API
(API_KEY_SERVICE_BLOCKED), so this authenticates as the logged-in gcloud
user instead. Run `gcloud auth application-default login` once before use.
"""

import logging

from google import genai
from google.genai import types
from google.auth.exceptions import DefaultCredentialsError

from speech_to_speech.config import GEMINI_LOCATION, GEMINI_MODEL, GEMINI_PROJECT, GEMINI_SYSTEM_INSTRUCTION

logger = logging.getLogger(__name__)


class GeminiConversation:
    def __init__(self) -> None:
        try:
            self._client = genai.Client(
                vertexai=True, project=GEMINI_PROJECT, location=GEMINI_LOCATION
            )
        except DefaultCredentialsError as exc:
            raise RuntimeError(
                "No Google Application Default Credentials found. Run "
                "`gcloud auth application-default login` once, then retry."
            ) from exc
        self._chat = self._client.chats.create(
            model=GEMINI_MODEL,
            config=types.GenerateContentConfig(
                system_instruction=GEMINI_SYSTEM_INSTRUCTION,
            ),
        )

    def send(self, text: str) -> str:
        logger.info("Sending to Gemini: %s", text)
        response = self._chat.send_message(text)
        reply = (response.text or "").strip()
        logger.info("Gemini reply: %s", reply)
        return reply
