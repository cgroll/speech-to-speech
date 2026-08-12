"""`parakeet-dictate` command: enable/disable/status/toggle.

enable/disable control the systemd user service (i.e. whether the model is
resident on the GPU at all); toggle talks to the running daemon over its Unix
socket and is what the GNOME hotkey invokes. toggle never auto-starts the
service -- explicit control is the point.
"""

import argparse
import subprocess
import sys

from speech_to_speech.dictate import feedback
from speech_to_speech.dictate.protocol import send_command

SERVICE_NAME = "parakeet-dictate.service"


def cmd_enable(_args: argparse.Namespace) -> int:
    subprocess.run(["systemctl", "--user", "start", SERVICE_NAME], check=True)
    print("Parakeet dictation enabled (model loading onto GPU).")
    return 0


def cmd_disable(_args: argparse.Namespace) -> int:
    subprocess.run(["systemctl", "--user", "stop", SERVICE_NAME], check=True)
    print("Parakeet dictation disabled (GPU memory freed).")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    result = send_command({"cmd": "status"})
    if result is None:
        print("not running (enable with `parakeet-dictate enable`)")
        return 1
    print(f"running, state={result.get('state')}")
    return 0


def cmd_toggle(_args: argparse.Namespace) -> int:
    result = send_command({"cmd": "toggle_record"})
    if result is None:
        feedback.notify(
            "Not enabled -- run `parakeet-dictate enable` first", error=True
        )
        return 1
    if not result.get("ok"):
        feedback.notify(f"Error: {result.get('error')}", error=True)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="parakeet-dictate")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("enable", help="Load the model onto the GPU").set_defaults(
        func=cmd_enable
    )
    sub.add_parser("disable", help="Unload the model, freeing GPU memory").set_defaults(
        func=cmd_disable
    )
    sub.add_parser("status", help="Show whether the daemon is running").set_defaults(
        func=cmd_status
    )
    sub.add_parser(
        "toggle", help="Start/stop a recording (bind this to your hotkey)"
    ).set_defaults(func=cmd_toggle)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
