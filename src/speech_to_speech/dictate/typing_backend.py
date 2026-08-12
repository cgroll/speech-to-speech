"""Types text into the currently focused window via ydotool.

Always invoked as an argument list (never shell=True) since the transcribed
text is uncontrolled input that could contain shell metacharacters.
"""

import logging
import subprocess

logger = logging.getLogger(__name__)


class TypingError(RuntimeError):
    pass


def type_text(text: str) -> None:
    if not text:
        return
    try:
        subprocess.run(
            ["ydotool", "type", "--key-delay", "1", "--", text],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise TypingError("ydotool is not installed") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace") if exc.stderr else ""
        raise TypingError(f"ydotool failed: {stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise TypingError("ydotool timed out") from exc
