"""User-facing feedback: notifications and sound cues, using tools already
present on the system (notify-send, canberra-gtk-play on Linux; afplay,
osascript on macOS) -- no bundled audio assets or extra dependencies needed."""

import logging
import subprocess
import sys

logger = logging.getLogger(__name__)

_SOUND_IDS_LINUX = {
    "start": "message-new-instant",
    "stop": "complete",
    "error": "dialog-warning",
}

_SOUNDS_MACOS = {
    "start": "/System/Library/Sounds/Tink.aiff",
    "stop": "/System/Library/Sounds/Glass.aiff",
    "error": "/System/Library/Sounds/Basso.aiff",
}


def _run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=False, capture_output=True, timeout=5)
    except FileNotFoundError:
        logger.debug("Feedback command not found: %s", cmd[0])


def play_cue(name: str) -> None:
    if sys.platform == "darwin":
        sound_path = _SOUNDS_MACOS.get(name)
        if sound_path:
            _run(["afplay", sound_path])
        return

    sound_id = _SOUND_IDS_LINUX.get(name)
    if sound_id is None:
        return
    _run(["canberra-gtk-play", "-i", sound_id])


def notify(message: str, *, error: bool = False) -> None:
    if sys.platform == "darwin":
        clean_msg = message.replace('"', '\\"')
        script = f'display notification "{clean_msg}" with title "Parakeet Dictate"'
        _run(["osascript", "-e", script])
        return

    args = ["notify-send", "Parakeet Dictate", message]
    if error:
        args += ["-u", "critical"]
    _run(args)
