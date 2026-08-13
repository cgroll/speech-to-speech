"""Client for the shared TTS daemon (speech_to_speech.tts_daemon.daemon),
used by the voice-chat pipeline (app.py) instead of loading its own Qwen3-TTS
model and driving playback directly -- see docs/architecture-proposal.md,
"Daemon-Aufspaltung" steps 5-6.

The daemon must already be running (`qwen-tts enable`); this module never
starts or stops it itself, same "explicit control, no auto-start" principle
as stt_client.py.
"""

import logging

import numpy as np

from speech_to_speech.dictate.protocol import decode_audio
from speech_to_speech.tts_daemon.protocol import send_command

logger = logging.getLogger(__name__)

# speak() blocks for the full playback duration -- TTS_MAX_NEW_TOKENS caps
# generation at several minutes worst case (see config.py), so this needs
# much more headroom than a quick status/stop round-trip.
_SPEAK_TIMEOUT_S = 600.0


class DaemonUnavailableError(RuntimeError):
    """Raised when the TTS daemon isn't reachable over its Unix socket."""


def ensure_available() -> None:
    """Fails fast if the TTS daemon isn't reachable, so App.load() can
    surface a clear error before the app starts listening for a toggle --
    same "fail before things get confusing later" pattern as
    stt_client.ensure_available()."""
    result = send_command({"cmd": "status"})
    if result is None:
        raise DaemonUnavailableError(
            "TTS daemon not running -- start it with `qwen-tts enable` first."
        )
    logger.info("TTS daemon reachable (state=%s).", result.get("state"))


def speak(text: str) -> float | None:
    """Blocks until playback finishes or is cancelled via stop(). Returns
    the time-to-first-audio in seconds, or None if playback was cancelled
    before any audio was played."""
    result = send_command({"cmd": "speak", "text": text}, timeout=_SPEAK_TIMEOUT_S)
    if result is None:
        raise DaemonUnavailableError("TTS daemon not running -- run `qwen-tts enable`.")
    if not result.get("ok"):
        raise RuntimeError(f"TTS daemon error: {result.get('error')}")
    return result.get("first_chunk_s")


def synthesize(text: str) -> tuple[np.ndarray, int]:
    """Synthesizes text to audio and returns it as (samples, sample_rate)
    instead of playing it on this machine's speakers -- for remote callers
    (e.g. the Gradio cockpit reached over Tailscale from a phone) that need
    the audio bytes to hand to a browser player. Blocks until synthesis
    finishes; unlike speak(), nothing is played here. A first, simple
    (non-streaming) round trip -- see docs/architecture-proposal.md,
    "Offene Frage: mobiler Zugriff (Handy)"."""
    result = send_command({"cmd": "synthesize", "text": text}, timeout=_SPEAK_TIMEOUT_S)
    if result is None:
        raise DaemonUnavailableError("TTS daemon not running -- run `qwen-tts enable`.")
    if not result.get("ok"):
        raise RuntimeError(f"TTS daemon error: {result.get('error')}")
    audio = decode_audio(result["audio"]) if result.get("audio") else np.zeros(0, dtype=np.float32)
    return audio, result.get("sample_rate") or 0


def stop() -> None:
    """Aborts whatever speak() call is currently in flight, if any (e.g. on
    barge-in). Talks over a fresh connection, independent of the one
    blocked inside speak()."""
    result = send_command({"cmd": "stop"}, timeout=5.0)
    if result is None:
        raise DaemonUnavailableError("TTS daemon not running -- run `qwen-tts enable`.")
