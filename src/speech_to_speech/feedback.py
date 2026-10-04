"""Audio feedback cue for recording start/stop, played on the Jabra-button
toggle path (app.py) so the user gets clear, immediate confirmation that the
press actually landed -- without needing to watch the cockpit (the Gradio
web UI isn't always in view, e.g. when the button is pressed from across the
room).

Spoken words ("Start"/"Ende"), not a tone: a short beep is easy to miss, or
to misjudge as the wrong transition, when you're not looking at the screen.
A word removes that ambiguity. Uses the OS's own instant local TTS
(espeak-ng on Linux, `say` on macOS) rather than this app's own Qwen3-TTS
daemon -- that model's latency (plus GPU/queue contention with a response
that might already be in flight, see docs/specs/tts-output-queue.md) would
defeat the purpose of fast feedback.

A third cue, "error", speaks "Fehler" when a toggle press was received but
the action it should have triggered (starting/stopping the STT daemon's
recording) failed -- e.g. the daemon reports "busy: recording" because it
was already in a recording state the app didn't expect, or the daemon is
unreachable. Without this, a button press that fails silently is
indistinguishable from one that worked: the button still feels responsive
(or the Jabra listener even shows as "unavailable" in the logs afterwards),
but app.py never got the state change it was reacting to, so "Start" was
never spoken in the first place -- that silence (no cue at all) *is* the
error signal to listen for here, and "Fehler" makes it explicit instead of
just absent.

Fire-and-forget: play_cue() returns immediately (runs the actual synthesis
in a daemon thread) so it never delays the mic start / state transition it
is signalling.

Guaranteed-audible, not "best effort": the whole point of this module is
that the user, who isn't looking at the screen, can tell from sound alone
whether their press landed. A spoken cue that silently fails (say, espeak-ng
crashes, or the sound server hiccups) used to be indistinguishable from
"no press happened" -- exactly the ambiguity this module exists to remove.
So _speak() now falls back to a plain tone (canberra-gtk-play / afplay on a
system sound) if the spoken word fails, and falls back again to a
desktop notification if even that fails -- three independent channels, so
the user gets *something* for every call site, not just when the primary
TTS binary happens to be healthy. Every failed attempt is logged at
warning level so these fallbacks show up in the daemon's logs instead of
vanishing.
"""

import logging
import subprocess
import sys
import threading
import time

logger = logging.getLogger(__name__)

# Brief head start before the *first* playback attempt (not before the
# retry -- that one already follows right after a failure, which is itself
# enough of a gap), giving a suspended output device -- e.g. a wireless
# headset's USB dock that idle-suspends to save the headset's own battery,
# see _speak() below -- a moment to wake before we actually need it. This
# doesn't delay the recording itself (already running by the time play_cue()
# is called, see app.py's _start_recording()), only how soon the cue sound
# starts, so it's pure UX: fewer races into the retry path, no change to
# when input is captured.
_WAKE_DELAY_S = 0.2

# German words, matching the rest of the app's user-facing strings (cockpit
# labels in app.py's STATE_LABELS are German too).
#
# "agent_start"/"agent_done" are deliberately different words from
# "start"/"stop" (which already mean "recording started/stopped") so the
# two pairs can't be confused: "Verstanden" fires once the transcribed/typed
# input has actually reached the agent (app.py's _respond()), "Fertig" once
# the agent's reply text is ready -- so the user knows no further input is
# expected and only the TTS synthesis/playback is left to wait for.
_WORDS = {
    "start": "Start",
    "stop": "Ende",
    "error": "Fehler",
    "agent_start": "Verstanden",
    "agent_done": "Fertig",
}

# Fallback tone, used only if the spoken word fails outright (see _speak).
_SOUND_IDS_LINUX = {
    "start": "message-new-instant",
    "stop": "complete",
    "error": "dialog-warning",
    "agent_start": "dialog-information",
    "agent_done": "bell",
}
_SOUNDS_MACOS = {
    "start": "/System/Library/Sounds/Tink.aiff",
    "stop": "/System/Library/Sounds/Glass.aiff",
    "error": "/System/Library/Sounds/Basso.aiff",
    "agent_start": "/System/Library/Sounds/Pop.aiff",
    "agent_done": "/System/Library/Sounds/Ping.aiff",
}


def _run(cmd: list[str]) -> bool:
    """Runs `cmd`, returning True only on a clean, zero-exit run. Every
    failure mode (missing binary, non-zero exit, timeout) is logged so a
    broken feedback channel shows up in the logs instead of just going
    quiet -- callers use the return value to decide whether to fall back
    to the next channel."""
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, timeout=5)
    except FileNotFoundError:
        logger.warning("Feedback command not found: %s", cmd[0])
        return False
    except subprocess.TimeoutExpired:
        logger.warning("Feedback command timed out: %s", cmd)
        return False
    if result.returncode != 0:
        logger.warning(
            "Feedback command %s exited %d: %s",
            cmd, result.returncode, result.stderr.decode(errors="replace").strip(),
        )
        return False
    return True


def _speak(word: str, name: str) -> None:
    if sys.platform == "darwin":
        # macOS ships no German voice by default; "Anna" does and reads
        # these two short words intelligibly enough.
        cmd = ["say", "-v", "Anna", word]
    else:
        cmd = ["espeak-ng", "-v", "de", word]

    time.sleep(_WAKE_DELAY_S)
    spoken = _run(cmd)
    if not spoken:
        # The output device (e.g. a wireless headset's USB dock) auto-
        # suspends after a few seconds of silence to save the headset's
        # own battery -- deliberately not disabled, since this machine
        # stays plugged in but the headset doesn't. Opening a new stream
        # normally wakes it, but the very first attempt right after an
        # idle period can lose that race and fail while the device is
        # still waking up. One immediate retry lands on an already-awake
        # device and clears this without touching the power-saving policy.
        logger.warning("Spoken cue '%s' failed, retrying once (device may be waking from idle-suspend).", word)
        spoken = _run(cmd)
    if spoken:
        return

    logger.warning("Spoken cue '%s' failed twice, falling back to a tone.", word)
    if sys.platform == "darwin":
        sound_path = _SOUNDS_MACOS.get(name)
        toned = bool(sound_path) and _run(["afplay", sound_path])
    else:
        sound_id = _SOUND_IDS_LINUX.get(name)
        toned = bool(sound_id) and _run(["canberra-gtk-play", "-i", sound_id])
    if toned:
        return

    # Last resort: both audio channels failed (no sound server reachable at
    # all, most likely). A silent press is exactly the ambiguity this module
    # exists to prevent, so fall back to something visible even though the
    # whole point of this cue was to not require looking at a screen.
    logger.warning("Fallback tone for '%s' also failed, falling back to a desktop notification.", word)
    if sys.platform == "darwin":
        script = f'display notification "{word}" with title "Speech-to-Speech"'
        _run(["osascript", "-e", script])
    else:
        _run(["notify-send", "Speech-to-Speech", word, "-u", "critical"])


def play_cue(name: str) -> None:
    """Speaks the cue word for `name` ("start"/"stop"/"error") in a
    background thread, falling back to a tone and then a desktop
    notification if speech fails (see module docstring). Unknown names are
    ignored (no-op), not an error, so callers on the hot button-press path
    never need a try/except around this."""
    word = _WORDS.get(name)
    if word is None:
        return
    threading.Thread(target=_speak, args=(word, name), daemon=True, name="feedback-cue").start()
