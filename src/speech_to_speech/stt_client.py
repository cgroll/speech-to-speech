"""Client for the shared STT daemon (speech_to_speech.dictate.daemon),
used by the voice-chat pipeline (app.py) instead of loading its own
Parakeet model and owning the mic directly -- see
docs/architecture-proposal.md, "Daemon-Aufspaltung" step 4.

The daemon must already be running (`parakeet-dictate enable`); this
module never starts or stops it itself, same "explicit control, no
auto-start" principle as dictate/cli.py's toggle command. It talks to the
daemon's "silent" start_recording/stop_recording commands (see
dictate/daemon.py), which mirror the hotkey's toggle_record mechanics but
return the transcript instead of typing it via ydotool.
"""

import logging

from speech_to_speech.dictate.protocol import send_command

logger = logging.getLogger(__name__)

# Draining + transcribing the tail of a long recording can take a few
# seconds after the mic is stopped; generous timeout so a slow final
# segment doesn't get mistaken for a dead daemon.
_STOP_TIMEOUT_S = 60.0


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
    result = send_command({"cmd": "start_recording"})
    if result is None:
        raise DaemonUnavailableError("STT daemon not running -- run `parakeet-dictate enable`.")
    if not result.get("ok"):
        raise RuntimeError(f"STT daemon error: {result.get('error')}")


def stop_recording() -> str:
    result = send_command({"cmd": "stop_recording"}, timeout=_STOP_TIMEOUT_S)
    if result is None:
        raise DaemonUnavailableError("STT daemon not running -- run `parakeet-dictate enable`.")
    if not result.get("ok"):
        raise RuntimeError(f"STT daemon error: {result.get('error')}")
    return result.get("text", "")
