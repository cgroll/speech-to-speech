"""Central constants. Values here are hardcoded for this specific machine
and setup (Jabra Link 390 keycode, chosen voice) rather than made
configurable -- this is a single-user PoC, not a distributable tool."""

import os
from pathlib import Path

# Repo root, derived from this file's location rather than the process's cwd
# (which may differ, e.g. when launched via systemd) -- used to scope past
# Claude Agent SDK sessions to this project (see sessions.py).
PROJECT_DIR = Path(__file__).resolve().parents[2]

# Default working directory for a new agent session (Claude SDK's `cwd`, Pi
# subprocess's `cwd`) -- see docs/backlog.md, "Mehrere Agent-Backends", point
# 2. Just the starting value; changeable per session (cockpit folder picker,
# Telegram `/workspace`), same "picked once at session start" model as the
# agent choice itself (agent_backend.py).
DEFAULT_WORKSPACE = PROJECT_DIR

# -- Jabra push-to-talk button -----------------------------------------
JABRA_DEVICE_NAME = "Jabra Link 390"

# Identified via `uv run python scripts/identify_jabra_key.py` -- the puck's
# button sends KEY_PLAY (code 207), not KEY_PLAYPAUSE.
JABRA_TOGGLE_KEY = "KEY_PLAY"

# macOS: CGEventTap (input_button.py) only sees system-wide Play/Pause media
# keys, not per-device identity -- NX_KEYTYPE_PLAY = 16 (IOKit/hidsystem/
# ev_keymap.h). Any source of a system Play key triggers the toggle, not just
# the Jabra (see docs/macos-setup.md).
JABRA_TOGGLE_KEY_MACOS = 16

# -- STT (Parakeet) ------------------------------------------------------
# Used by the STT daemon (dictate/daemon.py) that owns the model and mic -- see
# docs/architecture-proposal.md, "Daemon-Aufspaltung" step 4. The id differs by
# backend: onnx_asr (Linux, CPU) uses the "nemo-..." hub id; nano-parakeet
# (macOS, MPS) uses the plain Hugging Face id. daemon.py picks the right one.
STT_MODEL_NAME_ONNX = "nemo-parakeet-tdt-0.6b-v3"
STT_MODEL_NAME_NANO = "nvidia/parakeet-tdt-0.6b-v3"

# -- TTS (Qwen3-TTS CustomVoice, GPU) ------------------------------------
TTS_MODEL_ID = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
TTS_SPEAKER = "aiden"
TTS_LANGUAGE = "german"

# Local GGUF files for the GGML/Metal backend (macOS). On this machine HF's Xet
# download is blocked by the corporate proxy (see docs/macos-setup.md), so the
# talker + tokenizer GGUFs are placed here by hand and loaded directly, skipping
# the download. The CustomVoice speakers (incl. "aiden") are baked into the
# talker GGUF, so these two files are everything the backend needs. If both are
# present, tts.py loads from them; otherwise it falls back to the HF pull (the
# Linux/CUDA path never looks here). Filenames follow the BF16 quant.
TTS_GGUF_DIR = Path(
    os.environ.get("TTS_GGUF_DIR", os.path.expanduser("~/.config/speech-to-speech/models"))
)
TTS_GGUF_TALKER = "qwen-talker-0.6b-customvoice-BF16.gguf"
TTS_GGUF_TOKENIZER = "qwen-tokenizer-12hz-BF16.gguf"

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

# The talker generates audio autoregressively under that fixed KV-cache budget.
# A single long reply can exceed it: on CUDA the decode loop just stops and
# silently truncates mid-sentence (see the warning in tts.py), on the macOS
# ggml/Metal backend it crashes outright ("tts_engine_step: talker decode
# failed"). So we split the reply into sentence-sized chunks and synthesize them
# back to back, keeping every single generation well under the ceiling. A short
# reply -- the common case for a voice assistant -- stays one chunk, so this is
# a no-op there. Budget is in chars (a safe proxy for frames): ~3760 chars still
# generated fine, ~7520 crashed, so 1500 leaves a wide margin.
TTS_CHUNK_CHAR_BUDGET = 1500

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

# The Pi Coding Agent binary. If it's not in the PATH (e.g. when running via
# systemd without a full login environment), this can be set to an absolute
# path via the SPEECH_TO_SPEECH_PI_BIN environment variable.
PI_BIN = os.environ.get("SPEECH_TO_SPEECH_PI_BIN", "pi")

GEMINI_SYSTEM_INSTRUCTION = (
    "Du bist ein hilfreicher Sprachassistent in einem Sprach-zu-Sprach-Dialog. "
    "Antworte auf Deutsch, in kurzen, natürlich gesprochenen Sätzen. "
    "Verwende kein Markdown, keine Aufzählungszeichen, keine Code-Blöcke und "
    "keine Überschriften -- deine Antwort wird direkt per Text-to-Speech "
    "vorgelesen."
)

# -- Telegram-Bot (docs/telegram-bot-proposal.md) ------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
# Comma-separated chat ids allowed to talk to the bot; every other chat is
# silently ignored (telegram_bot/daemon.py). Kept as strings since that's
# what update.effective_chat.id gets compared against after str()'ing it.
TELEGRAM_ALLOWED_CHAT_IDS = {
    chat_id.strip()
    for chat_id in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
    if chat_id.strip()
}

# -- Web-Cockpit (Gradio) -------------------------------------------------
# Bound to localhost only -- the LLM backend runs with bypassPermissions and
# full tool access, so exposing this beyond the local machine needs a VPN or
# access-gated tunnel (see docs/architecture-proposal.md), not a config flag.
COCKPIT_HOST = "127.0.0.1"
COCKPIT_PORT = 7860
# Poll interval for the cockpit's live state/chat/stats refresh.
COCKPIT_POLL_SECONDS = 0.3
