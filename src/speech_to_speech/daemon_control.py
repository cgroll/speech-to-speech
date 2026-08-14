"""Manual restart for the STT/TTS daemons and the Telegram bot itself,
callable from within a running process (Gradio cockpit, Telegram bot)
instead of only from a terminal (`parakeet-dictate`/`qwen-tts`
enable/disable) -- see docs/backlog.md, "Daemon-Neustart aus der App/dem
Cockpit heraus". An escape hatch for when a daemon is still answering but
has gotten into a bad state, without waiting for (or without triggering)
the TTS daemon's own watchdog/`Restart=on-failure` (tts_daemon/daemon.py).

Same `systemctl --user` calls the CLIs' enable/disable already make (see
dictate/cli.py, tts_daemon/cli.py, telegram_bot/cli.py) -- `restart` instead
of separate stop+start, since systemd already makes that atomic.
"""

import subprocess

from speech_to_speech.dictate.cli import SERVICE_NAME as STT_SERVICE_NAME
from speech_to_speech.telegram_bot.cli import SERVICE_NAME as TELEGRAM_BOT_SERVICE_NAME
from speech_to_speech.tts_daemon.cli import SERVICE_NAME as TTS_SERVICE_NAME


def restart_stt_daemon() -> None:
    subprocess.run(["systemctl", "--user", "restart", STT_SERVICE_NAME], check=True)


def restart_tts_daemon() -> None:
    subprocess.run(["systemctl", "--user", "restart", TTS_SERVICE_NAME], check=True)


def restart_telegram_bot() -> None:
    """Unlike the two above, this restarts the very process calling it --
    systemd (`KillMode=control-group`, the default) tears down this process
    (and the `systemctl` child below) as part of the restart, so this call
    may never return. Callers that want to tell the user beforehand (the
    Telegram bot's own /restart_bot command) need to send that message
    first -- there's no reliable way to send one afterwards."""
    subprocess.run(["systemctl", "--user", "restart", TELEGRAM_BOT_SERVICE_NAME], check=True)
