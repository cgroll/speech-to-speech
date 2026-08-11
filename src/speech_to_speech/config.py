"""Central constants. Values here are hardcoded for this specific machine
and setup (Jabra Link 390 keycode, chosen voice) rather than made
configurable -- this is a single-user PoC, not a distributable tool."""

import os

# -- Jabra push-to-talk button -----------------------------------------
JABRA_DEVICE_NAME = "Jabra Link 390"

# Identified via `uv run python scripts/identify_jabra_key.py` -- the puck's
# button sends KEY_PLAY (code 207), not KEY_PLAYPAUSE.
JABRA_TOGGLE_KEY = "KEY_PLAY"

# -- STT (Parakeet, CPU) -------------------------------------------------
STT_MODEL_NAME = "nemo-parakeet-tdt-0.6b-v3"
STT_SAMPLE_RATE = 16_000

# -- TTS (Qwen3-TTS CustomVoice, GPU) ------------------------------------
TTS_MODEL_ID = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
TTS_SPEAKER = "aiden"
TTS_LANGUAGE = "german"

# Playback tempo for the synthesized reply (pitch-preserving time-stretch,
# not raw resampling -- 1.0 = natural speed).
TTS_PLAYBACK_SPEED = 1.5

# -- LLM (Gemini via Vertex AI) -------------------------------------------
# pi-agent-gemini's org policy blocks plain Gemini-Developer-API keys
# (API_KEY_SERVICE_BLOCKED) -- Vertex AI with Application Default
# Credentials (`gcloud auth application-default login`) is the path that
# works under that policy.
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_PROJECT = os.environ.get("GEMINI_PROJECT", "pi-agent-gemini")
GEMINI_LOCATION = os.environ.get("GEMINI_LOCATION", "us-central1")

GEMINI_SYSTEM_INSTRUCTION = (
    "Du bist ein hilfreicher Sprachassistent in einem Sprach-zu-Sprach-Dialog. "
    "Antworte auf Deutsch, in kurzen, natürlich gesprochenen Sätzen. "
    "Verwende kein Markdown, keine Aufzählungszeichen, keine Code-Blöcke und "
    "keine Überschriften -- deine Antwort wird direkt per Text-to-Speech "
    "vorgelesen."
)
