"""Diagnostic tool: prints every key event from the Jabra Link 390's input
device so you can press the button you want to use and read off its keycode.

Run after scripts/setup_jabra_input.sh has been applied and you've logged
back in. Press Ctrl+C to stop.

    uv run python scripts/identify_jabra_key.py
"""

from evdev import InputDevice, categorize, ecodes, list_devices

DEVICE_NAME = "Jabra Link 390"


def find_device() -> InputDevice:
    for path in list_devices():
        dev = InputDevice(path)
        if DEVICE_NAME in dev.name:
            return dev
    raise SystemExit(
        f"No input device named like '{DEVICE_NAME}' found. "
        "Is the Jabra plugged in, and did you apply scripts/setup_jabra_input.sh "
        "and log back in?"
    )


def main() -> None:
    dev = find_device()
    print(f"Listening on {dev.path} ({dev.name}). Press buttons on the Jabra puck, Ctrl+C to stop.")
    for event in dev.read_loop():
        if event.type != ecodes.EV_KEY:
            continue
        key_event = categorize(event)
        state = {0: "up", 1: "down", 2: "hold"}.get(key_event.keystate, str(key_event.keystate))
        print(f"code={event.code} name={key_event.keycode} state={state}")


if __name__ == "__main__":
    main()
