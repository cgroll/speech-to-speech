"""Reads the Jabra Link 390's button as a push-to-talk toggle: evdev on
Linux, a CGEventTap on macOS.

Linux: requires scripts/setup_jabra_input.sh to have been applied (and a
fresh login) so the process can read /dev/input/eventN for this device. The
device is opened non-exclusively (no grab()) -- any native OS handling of
the same key keeps working alongside this listener.

macOS: there is no evdev equivalent, and raw HID reads don't work either --
macOS' own HID event system already translates the button press into a
system-wide Play/Pause media key before any userspace HID client (even a
non-exclusive one) sees a report. So instead this taps NX_SYSDEFINED events
(CGEventTap) and filters for NX_KEYTYPE_PLAY, the same signal any app
watching for the system media key would see. Two consequences, both
accepted (see docs/macos-setup.md): the trigger is system-wide, not
Jabra-specific (any source of a Play key press toggles the app), and the key
keeps acting as a normal Play/Pause key at the same time -- an active tap
that swallows the event was tested and does NOT suppress it, since macOS'
MediaRemote system routes media keys to the frontmost "Now Playing" app
before an app-level tap gets a chance to consume it. Suppressing it for real
would need something lower in the stack, e.g. Karabiner-Elements.
"""

import logging
import sys
from typing import TYPE_CHECKING, Callable

from speech_to_speech.config import JABRA_DEVICE_NAME, JABRA_TOGGLE_KEY, JABRA_TOGGLE_KEY_MACOS

if TYPE_CHECKING:
    from evdev import InputDevice

logger = logging.getLogger(__name__)

_KEY_DOWN = 1

# NSEvent subtype for Aux Control (media/system) keys within an
# NX_SYSDEFINED event, and the key-down state nibble within data1's low word
# -- both fixed values from how macOS encodes these events, not configurable.
_NX_SYSDEFINED_SUBTYPE = 8
_NX_KEYSTATE_DOWN = 0x0A


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


def _import_macos_event_tap():
    """pyobjc-framework-Quartz/-Cocoa are darwin-only (sys_platform marker in
    pyproject.toml), so they aren't installed on Linux. Import lazily here,
    same reasoning as _import_evdev() above."""
    try:
        import Quartz
        from AppKit import NSEvent

        return Quartz, NSEvent
    except ImportError as exc:
        raise RuntimeError(
            "pyobjc-framework-Quartz/-Cocoa are not available. The Jabra button "
            "is not supported without them; use the local toggle hotkey instead."
        ) from exc


def _listen_for_toggle_macos(on_toggle: Callable[[], None]) -> None:
    Quartz, NSEvent = _import_macos_event_tap()

    def callback(proxy, event_type, event, refcon):
        if event_type in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
            logger.warning("macOS event tap was disabled (%s), re-enabling.", event_type)
            Quartz.CGEventTapEnable(tap, True)
            return event
        ns_event = NSEvent.eventWithCGEvent_(event)
        if ns_event.subtype() != _NX_SYSDEFINED_SUBTYPE:
            return event
        data1 = ns_event.data1()
        key_code = (data1 & 0xFFFF0000) >> 16
        key_down = ((data1 & 0xFF00) >> 8) == _NX_KEYSTATE_DOWN
        if key_code == JABRA_TOGGLE_KEY_MACOS and key_down:
            on_toggle()
        return event

    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly,
        Quartz.CGEventMaskBit(14),  # NX_SYSDEFINED
        callback,
        None,
    )
    if tap is None:
        raise RuntimeError(
            "CGEventTap could not be created. Grant Accessibility and/or Input "
            "Monitoring to this terminal in System Settings -> Privacy & "
            "Security, then restart the app."
        )

    logger.info("Listening for the system Play/Pause key (CGEventTap) on macOS")
    run_loop_source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), run_loop_source, Quartz.kCFRunLoopCommonModes)
    Quartz.CGEventTapEnable(tap, True)
    Quartz.CFRunLoopRun()


def listen_for_toggle(on_toggle: Callable[[], None]) -> None:
    """Blocks forever, calling on_toggle() each time the configured button
    is pressed (key-down only; releases and key-repeat are ignored)."""
    if sys.platform == "darwin":
        _listen_for_toggle_macos(on_toggle)
        return

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
