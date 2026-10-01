"""Types text into the currently focused window: ydotool on Linux, an
AppleScript Cmd+V paste on macOS.

Always invoked as an argument list (never shell=True) since the transcribed
text is uncontrolled input that could contain shell metacharacters.

The macOS path pastes via the clipboard rather than synthesising keystrokes:
it's layout-independent (so umlauts come through natively, no QWERTZ keymap --
see keymap.py, which the Linux ydotool path still needs) and far faster than
typing character by character. It saves and restores the clipboard around the
paste; the small window where a copy from elsewhere could be clobbered is
accepted (macOS port plan, risk 6).
"""

import logging
import subprocess
import sys
import time

logger = logging.getLogger(__name__)


class TypingError(RuntimeError):
    pass


def _type_text_macos(text: str) -> None:
    try:
        # Probe Accessibility permission first: without it the keystroke below
        # fails with an opaque error, so surface the actionable message instead.
        check_script = 'tell application "System Events" to get name'
        subprocess.run(["osascript", "-e", check_script], check=True, capture_output=True, timeout=5)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise TypingError(
            "System Events is not permitted. Grant Accessibility to your terminal "
            "(or the app launching this) in System Settings -> Privacy & Security "
            "-> Accessibility."
        ) from exc

    try:
        old_clipboard = subprocess.run(
            ["pbpaste"], capture_output=True, text=True, check=True
        ).stdout
    except Exception:
        old_clipboard = ""

    try:
        p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE, text=True)
        p.communicate(input=text)

        paste_script = 'tell application "System Events" to keystroke "v" using {command down}'
        subprocess.run(["osascript", "-e", paste_script], check=True, capture_output=True, timeout=5)

        # Let the paste land before we overwrite the clipboard again below.
        time.sleep(0.15)
    except Exception as exc:
        raise TypingError(f"macOS paste failed: {exc}") from exc
    finally:
        try:
            p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE, text=True)
            p.communicate(input=old_clipboard)
        except Exception:
            pass


def type_text(text: str) -> None:
    if not text:
        return

    if sys.platform == "darwin":
        _type_text_macos(text)
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
