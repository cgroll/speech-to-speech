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
# Used by the STT daemon (dictate/daemon.py) that now owns the model and mic
# -- see docs/architecture-proposal.md, "Daemon-Aufspaltung" step 4.
STT_MODEL_NAME = "nemo-parakeet-tdt-0.6b-v3"

# -- TTS (Qwen3-TTS CustomVoice, GPU) ------------------------------------
TTS_MODEL_ID = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
TTS_SPEAKER = "aiden"
TTS_LANGUAGE = "german"

# faster-qwen3-tts defaults both of these to 2048 if left unset, which caps
# generation at ~2048 codec frames (12Hz model -> well under 2048/12 = 170s,
# since max_seq_len also has to cover the text prefill). Hitting that ceiling
# is NOT an error -- the library's decode loop just stops and returns
# whatever audio it has, silently truncating mid-sentence. Raised here to
# give a few minutes of headroom; TTS_MAX_SEQ_LEN sizes the CUDA graph's
# static KV cache (paid once at warmup as extra GPU memory + a bit more
# capture time), so don't inflate it further than needed.
TTS_MAX_SEQ_LEN = 4096
TTS_MAX_NEW_TOKENS = 3900

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

# -- Web-Cockpit (Gradio) -------------------------------------------------
# Bound to localhost only -- the LLM backend runs with bypassPermissions and
# full tool access, so exposing this beyond the local machine needs a VPN or
# access-gated tunnel (see docs/architecture-proposal.md), not a config flag.
COCKPIT_HOST = "127.0.0.1"
COCKPIT_PORT = 7860
# Poll interval for the cockpit's live state/chat/stats refresh.
COCKPIT_POLL_SECONDS = 0.3
