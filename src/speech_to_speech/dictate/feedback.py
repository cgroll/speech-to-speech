"""User-facing feedback: notifications and sound cues, using tools already
present on the system (notify-send, canberra-gtk-play) -- no bundled audio
assets or extra dependencies needed."""

import logging
import subprocess

logger = logging.getLogger(__name__)

_SOUND_IDS = {
    "start": "message-new-instant",
    "stop": "complete",
    "error": "dialog-warning",
}


def _run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=False, capture_output=True, timeout=5)
    except FileNotFoundError:
        logger.debug("Feedback command not found: %s", cmd[0])


def play_cue(name: str) -> None:
    sound_id = _SOUND_IDS.get(name)
    if sound_id is None:
        return
    _run(["canberra-gtk-play", "-i", sound_id])


def notify(message: str, *, error: bool = False) -> None:
    args = ["notify-send", "Parakeet Dictate", message]
    if error:
        args += ["-u", "critical"]
    _run(args)
