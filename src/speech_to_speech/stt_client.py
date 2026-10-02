"""Client for the shared STT daemon (speech_to_speech.dictate.daemon),
used by the voice-chat pipeline (app.py) and the Telegram bot
(telegram_bot/daemon.py) instead of loading their own Parakeet model and
owning the mic directly -- see docs/architecture-proposal.md,
"Daemon-Aufspaltung" step 4, and docs/telegram-bot-proposal.md.

The daemon must already be running (`parakeet-dictate enable`); this
module never starts or stops it itself, same "explicit control, no
auto-start" principle as dictate/cli.py's toggle command. It talks to the
daemon's "silent" start_recording/stop_recording commands (see
dictate/daemon.py), which mirror the hotkey's toggle_record mechanics but
return the transcript instead of typing it via ydotool, plus `transcribe`
for already-recorded audio (no live mic involved at all).
"""

import logging

import numpy as np

from speech_to_speech.dictate.protocol import encode_audio, send_command

logger = logging.getLogger(__name__)

# Draining + transcribing the tail of a long recording can take a few
# seconds after the mic is stopped; generous timeout so a slow final
# segment doesn't get mistaken for a dead daemon.
_STOP_TIMEOUT_S = 60.0

# transcribe() is a single already-recorded utterance (a Telegram voice
# message), not an open-ended live recording -- much shorter than
# _STOP_TIMEOUT_S covers, but still generous relative to typical inference
# time for a few seconds/minutes of audio.
_TRANSCRIBE_TIMEOUT_S = 60.0

_is_recording = False


class DaemonUnavailableError(RuntimeError):
    """Raised when the STT daemon isn't reachable over its Unix socket."""


def ensure_available() -> None:
    """Fails fast if the STT daemon isn't reachable, so App.load() can
    surface a clear error before the rest of startup (TTS model) runs --
    same "fail before loading anything heavy" pattern as the missing-API
    -key check next to it."""
    result = send_command({"cmd": "status"})
    if result is None:
        raise DaemonUnavailableError(
            "STT daemon not running -- start it with `parakeet-dictate enable` first."
        )
    logger.info("STT daemon reachable (state=%s).", result.get("state"))


def start_recording() -> None:
    global _is_recording
    result = send_command({"cmd": "start_recording"})
    if result is None:
        raise DaemonUnavailableError("STT daemon not running -- run `parakeet-dictate enable`.")
    if not result.get("ok"):
        raise RuntimeError(f"STT daemon error: {result.get('error')}")
    _is_recording = True


def stop_recording() -> str:
    global _is_recording
    result = send_command({"cmd": "stop_recording"}, timeout=_STOP_TIMEOUT_S)
    if result is None:
        raise DaemonUnavailableError("STT daemon not running -- run `parakeet-dictate enable`.")
    if not result.get("ok"):
        raise RuntimeError(f"STT daemon error: {result.get('error')}")
    _is_recording = False
    return result.get("text", "")


def is_recording() -> bool:
    return _is_recording


def transcribe(audio: np.ndarray) -> str:
    """Transcribes already-recorded 16 kHz mono float32 audio -- no live
    mic/VAD involved (see dictate/daemon.py's `transcribe` command). Used by
    the Telegram bot for downloaded, ffmpeg-decoded voice messages."""
    result = send_command({"cmd": "transcribe", "audio": encode_audio(audio)}, timeout=_TRANSCRIBE_TIMEOUT_S)
    if result is None:
        raise DaemonUnavailableError("STT daemon not running -- run `parakeet-dictate enable`.")
    if not result.get("ok"):
        raise RuntimeError(f"STT daemon error: {result.get('error')}")
    return result.get("text", "")
