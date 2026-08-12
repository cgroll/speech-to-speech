"""`speech-to-speech-telegram-bot` command: start/stop/status for the
Telegram-bot systemd user service.

Unlike parakeet-dictate/qwen-tts, this service *does* auto-start at login
(`systemctl --user enable`, done once during setup -- see systemd/telegram
-bot.service) since its whole point is being reachable from a phone even
when nobody is at the desk. This CLI is just for manual override
(restarting after an `.env` change, checking liveness) -- it has no
`enable`/`disable` verbs of its own, to avoid confusion with systemd's own
enable/disable (which controls auto-start-at-login, not "running now").
"""

import argparse
import subprocess
import sys

SERVICE_NAME = "speech-to-speech-telegram-bot.service"


def cmd_start(_args: argparse.Namespace) -> int:
    subprocess.run(["systemctl", "--user", "start", SERVICE_NAME], check=True)
    print("Telegram bot started.")
    return 0


def cmd_stop(_args: argparse.Namespace) -> int:
    subprocess.run(["systemctl", "--user", "stop", SERVICE_NAME], check=True)
    print("Telegram bot stopped.")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", SERVICE_NAME], capture_output=True, text=True
    )
    print(result.stdout.strip() or result.stderr.strip())
    return 0 if result.returncode == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="speech-to-speech-telegram-bot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start", help="Start the bot now").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="Stop the bot").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="Show whether the bot is running").set_defaults(func=cmd_status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
