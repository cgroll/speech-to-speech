"""`qwen-tts` command: enable/disable/status for the shared TTS daemon.
Command name kept as `qwen-tts` for muscle memory even though the backend
underneath it is switchable (config.TTS_BACKEND) -- renaming it would touch
the systemd unit, daemon_launch.py's macOS path, and everyone's habits for
no functional gain.

enable/disable control the systemd user service (i.e. whether the model is
resident at all -- on GPU for the qwen3 backend, in regular RAM for the
default phonon backend); status is a quick liveness/state check. Unlike
`parakeet-dictate`'s CLI, there's no user-facing `toggle` here -- the
voice-chat app (app.py) talks to the daemon's speak/stop commands directly
via tts_client.py, not through this CLI.
"""

import argparse
import subprocess
import sys

# Must run before the speech_to_speech.config import below -- see
# daemon.py's identical comment for why (TTS_BACKEND is read from os.environ
# at config.py's module-import time).
from dotenv import load_dotenv

load_dotenv()

from speech_to_speech.config import TTS_BACKEND
from speech_to_speech.tts_daemon.protocol import send_command, socket_path

SERVICE_NAME = "qwen-tts.service"
# Entry point (pyproject [project.scripts]) used to launch/kill the daemon on
# macOS, where there's no systemd -- see daemon_launch.py.
DAEMON_ENTRY_POINT = "qwen-tts-daemon"


def cmd_enable(_args: argparse.Namespace) -> int:
    # Phonon (CPU) never touches the GPU/Metal, regardless of platform.
    where = "into RAM (CPU)" if TTS_BACKEND != "qwen3" else "onto Metal" if sys.platform == "darwin" else "onto the GPU"
    if sys.platform == "darwin":
        from speech_to_speech import daemon_launch

        daemon_launch.start(DAEMON_ENTRY_POINT, socket_path())
        print(f"TTS daemon launched ({TTS_BACKEND}, model loading {where}).")
        return 0

    subprocess.run(["systemctl", "--user", "start", SERVICE_NAME], check=True)
    print(f"TTS daemon enabled ({TTS_BACKEND}, model loading {where}).")
    return 0


def cmd_disable(_args: argparse.Namespace) -> int:
    freed = "GPU memory" if TTS_BACKEND == "qwen3" else "RAM"
    if sys.platform == "darwin":
        from speech_to_speech import daemon_launch

        daemon_launch.stop(DAEMON_ENTRY_POINT, socket_path())
        print("TTS daemon disabled.")
        return 0

    subprocess.run(["systemctl", "--user", "stop", SERVICE_NAME], check=True)
    print(f"TTS daemon disabled ({freed} freed).")
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
