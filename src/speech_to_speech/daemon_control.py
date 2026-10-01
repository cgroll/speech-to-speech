"""Manual restart for the STT/TTS daemons and the Telegram bot itself,
callable from within a running process (Gradio cockpit, Telegram bot)
instead of only from a terminal (`parakeet-dictate`/`qwen-tts`
enable/disable) -- see docs/backlog.md, "Daemon-Neustart aus der App/dem
Cockpit heraus". An escape hatch for when a daemon is still answering but
has gotten into a bad state, without waiting for (or without triggering)
the TTS daemon's own watchdog/`Restart=on-failure` (tts_daemon/daemon.py).

On Linux this is a `systemctl --user restart`; on macOS (no systemd) it's the
pkill+Popen restart from daemon_launch.py, the same mechanism the CLIs'
enable/disable use there.
"""

import subprocess
import sys

from speech_to_speech.dictate.cli import DAEMON_ENTRY_POINT as STT_ENTRY_POINT
from speech_to_speech.dictate.cli import SERVICE_NAME as STT_SERVICE_NAME
from speech_to_speech.dictate.protocol import socket_path as stt_socket_path
from speech_to_speech.telegram_bot.cli import SERVICE_NAME as TELEGRAM_BOT_SERVICE_NAME
from speech_to_speech.tts_daemon.cli import DAEMON_ENTRY_POINT as TTS_ENTRY_POINT
from speech_to_speech.tts_daemon.cli import SERVICE_NAME as TTS_SERVICE_NAME
from speech_to_speech.tts_daemon.protocol import socket_path as tts_socket_path


def restart_stt_daemon() -> None:
    if sys.platform == "darwin":
        from speech_to_speech import daemon_launch

        daemon_launch.restart(STT_ENTRY_POINT, stt_socket_path())
        return
    subprocess.run(["systemctl", "--user", "restart", STT_SERVICE_NAME], check=True)


def restart_tts_daemon() -> None:
    if sys.platform == "darwin":
        from speech_to_speech import daemon_launch

        daemon_launch.restart(TTS_ENTRY_POINT, tts_socket_path())
        return
    subprocess.run(["systemctl", "--user", "restart", TTS_SERVICE_NAME], check=True)


def restart_telegram_bot() -> None:
    """Unlike the two above, this restarts the very process calling it --
    systemd (`KillMode=control-group`, the default) tears down this process
    (and the `systemctl` child below) as part of the restart, so this call
    may never return. Callers that want to tell the user beforehand (the
    Telegram bot's own /restart_bot command) need to send that message
    first -- there's no reliable way to send one afterwards."""
    # macOS: the Telegram bot isn't part of the current scope and has no
    # Popen-based launch path here, so leave the systemd call as the Linux-only
    # behaviour rather than fake a self-restart without a supervisor.
    subprocess.run(["systemctl", "--user", "restart", TELEGRAM_BOT_SERVICE_NAME], check=True)
