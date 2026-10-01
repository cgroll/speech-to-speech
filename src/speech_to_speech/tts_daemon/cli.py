"""`qwen-tts` command: enable/disable/status for the shared TTS daemon.

enable/disable control the systemd user service (i.e. whether the model is
resident on the GPU at all); status is a quick liveness/state check. Unlike
`parakeet-dictate`'s CLI, there's no user-facing `toggle` here -- the
voice-chat app (app.py) talks to the daemon's speak/stop commands directly
via tts_client.py, not through this CLI.
"""

import argparse
import subprocess
import sys

from speech_to_speech.tts_daemon.protocol import send_command, socket_path

SERVICE_NAME = "qwen-tts.service"
# Entry point (pyproject [project.scripts]) used to launch/kill the daemon on
# macOS, where there's no systemd -- see daemon_launch.py.
DAEMON_ENTRY_POINT = "qwen-tts-daemon"


def cmd_enable(_args: argparse.Namespace) -> int:
    if sys.platform == "darwin":
        from speech_to_speech import daemon_launch

        daemon_launch.start(DAEMON_ENTRY_POINT, socket_path())
        print("Qwen3-TTS daemon launched (model loading onto Metal).")
        return 0

    subprocess.run(["systemctl", "--user", "start", SERVICE_NAME], check=True)
    print("Qwen3-TTS daemon enabled (model loading onto GPU).")
    return 0


def cmd_disable(_args: argparse.Namespace) -> int:
    if sys.platform == "darwin":
        from speech_to_speech import daemon_launch

        daemon_launch.stop(DAEMON_ENTRY_POINT, socket_path())
        print("Qwen3-TTS daemon disabled.")
        return 0

    subprocess.run(["systemctl", "--user", "stop", SERVICE_NAME], check=True)
    print("Qwen3-TTS daemon disabled (GPU memory freed).")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    result = send_command({"cmd": "status"})
    if result is None:
        print("not running (enable with `qwen-tts enable`)")
        return 1
    print(f"running, state={result.get('state')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="qwen-tts")
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

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
