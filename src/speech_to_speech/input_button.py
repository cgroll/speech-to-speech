"""Reads the Jabra Link 390's button as a push-to-talk toggle, via evdev.

Requires scripts/setup_jabra_input.sh to have been applied (and a fresh
login) so the process can read /dev/input/eventN for this device. The
device is opened non-exclusively (no grab()) -- any native OS handling of
the same key keeps working alongside this listener.
"""

import logging
from typing import TYPE_CHECKING, Callable

from speech_to_speech.config import JABRA_DEVICE_NAME, JABRA_TOGGLE_KEY

if TYPE_CHECKING:
    from evdev import InputDevice

logger = logging.getLogger(__name__)

_KEY_DOWN = 1


def _import_evdev():
    """evdev is Linux-only (needs <linux/input.h>) and is dependency-gated to
    Linux in pyproject.toml, so it isn't installed on macOS. Import it lazily
    here rather than at module load, so this module stays importable on macOS
    (app.py imports it unconditionally) -- the Jabra path then self-disables at
    runtime via the RuntimeError below, caught by app.py's _run_jabra_listener."""
    try:
        import evdev

        return evdev
    except ImportError as exc:
        raise RuntimeError(
            "evdev is not available (Linux-only). The Jabra button is not "
            "supported on this platform; use the local toggle hotkey instead."
        ) from exc


def find_jabra_device() -> "InputDevice":
    evdev = _import_evdev()
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        if JABRA_DEVICE_NAME in dev.name:
            return dev
    raise RuntimeError(
        f"No input device named like '{JABRA_DEVICE_NAME}' found. Is the Jabra "
        "plugged in, and did you apply scripts/setup_jabra_input.sh and log back in?"
    )


def listen_for_toggle(on_toggle: Callable[[], None]) -> None:
    """Blocks forever, calling on_toggle() each time the configured button
    is pressed (key-down only; releases and key-repeat are ignored)."""
    evdev = _import_evdev()
    device = find_jabra_device()
    logger.info("Listening for %s on %s", JABRA_TOGGLE_KEY, device.path)
    for event in device.read_loop():
        if event.type != evdev.ecodes.EV_KEY:
            continue
        key_event = evdev.categorize(event)
        if key_event.keystate != _KEY_DOWN:
            continue
        keycodes = key_event.keycode
        if isinstance(keycodes, str):
            keycodes = [keycodes]
        if JABRA_TOGGLE_KEY in keycodes:
            on_toggle()
