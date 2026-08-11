"""Reads the Jabra Link 390's button as a push-to-talk toggle, via evdev.

Requires scripts/setup_jabra_input.sh to have been applied (and a fresh
login) so the process can read /dev/input/eventN for this device. The
device is opened non-exclusively (no grab()) -- any native OS handling of
the same key keeps working alongside this listener.
"""

import logging
from typing import Callable

from evdev import InputDevice, categorize, ecodes, list_devices

from speech_to_speech.config import JABRA_DEVICE_NAME, JABRA_TOGGLE_KEY

logger = logging.getLogger(__name__)

_KEY_DOWN = 1


def find_jabra_device() -> InputDevice:
    for path in list_devices():
        dev = InputDevice(path)
        if JABRA_DEVICE_NAME in dev.name:
            return dev
    raise RuntimeError(
        f"No input device named like '{JABRA_DEVICE_NAME}' found. Is the Jabra "
        "plugged in, and did you apply scripts/setup_jabra_input.sh and log back in?"
    )


def listen_for_toggle(on_toggle: Callable[[], None]) -> None:
    """Blocks forever, calling on_toggle() each time the configured button
    is pressed (key-down only; releases and key-repeat are ignored)."""
    device = find_jabra_device()
    logger.info("Listening for %s on %s", JABRA_TOGGLE_KEY, device.path)
    for event in device.read_loop():
        if event.type != ecodes.EV_KEY:
            continue
        key_event = categorize(event)
        if key_event.keystate != _KEY_DOWN:
            continue
        keycodes = key_event.keycode
        if isinstance(keycodes, str):
            keycodes = [keycodes]
        if JABRA_TOGGLE_KEY in keycodes:
            on_toggle()
