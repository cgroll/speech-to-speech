import argparse
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)


def _parse_args() -> argparse.Namespace:
    # Imported here rather than at module level for the same reason as the
    # lazy imports in main() below -- main_toggle() doesn't need this either.
    from speech_to_speech.agent_backend import AGENT_LABELS, DEFAULT_AGENT

    parser = argparse.ArgumentParser(prog="speech-to-speech")
    parser.add_argument(
        "--agent",
        choices=sorted(AGENT_LABELS),
        default=DEFAULT_AGENT,
        help=f"LLM backend for the first session (default: {DEFAULT_AGENT}).",
    )
    return parser.parse_args()


def main() -> None:
    # Imported lazily so `main_toggle` (bound to a hotkey, called often)
    # doesn't pay for torch/model-related imports it never needs.
    from dotenv import load_dotenv

    from speech_to_speech.app import App

    args = _parse_args()
    load_dotenv()
    app = App(default_agent=args.agent)
    try:
        app.load()
        app.run()
    except KeyboardInterrupt:
        print("\nBye.")
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def main_start() -> None:
    """Convenience entry point: starts both shared daemons (idempotent -- a
    no-op if one is already running) and waits until each is actually reachable
    over its socket, not just until the launch call returns -- model loading
    takes a few seconds to a couple minutes after that. Then launches the app.
    One command instead of the usual three (`parakeet-dictate enable`,
    `qwen-tts enable`, `speech-to-speech`).

    Linux starts the daemons via `systemctl --user`; macOS (no systemd) via the
    Popen launch in daemon_launch.py. Either way the socket-wait loop below is
    what actually gates on readiness, so both paths share it."""
    import subprocess
    import time

    from speech_to_speech.dictate.cli import DAEMON_ENTRY_POINT as STT_ENTRY_POINT
    from speech_to_speech.dictate.cli import SERVICE_NAME as STT_SERVICE
    from speech_to_speech.dictate.protocol import send_command as stt_status
    from speech_to_speech.dictate.protocol import socket_path as stt_socket_path
    from speech_to_speech.tts_daemon.cli import DAEMON_ENTRY_POINT as TTS_ENTRY_POINT
    from speech_to_speech.tts_daemon.cli import SERVICE_NAME as TTS_SERVICE
    from speech_to_speech.tts_daemon.protocol import send_command as tts_status
    from speech_to_speech.tts_daemon.protocol import socket_path as tts_socket_path

    def _launch(service: str, entry_point: str, socket_path_fn) -> None:
        if sys.platform == "darwin":
            from speech_to_speech import daemon_launch

            daemon_launch.start(entry_point, socket_path_fn())
            return
        subprocess.run(["systemctl", "--user", "start", service], check=True)

    def _start_and_wait(
        label: str, service: str, entry_point: str, socket_path_fn, status_check, timeout_s: float
    ) -> None:
        # Idempotency on macOS: if the socket already answers, don't relaunch
        # (a second Popen would unlink the live socket). systemctl is idempotent
        # on its own, so the guard is darwin-only.
        if sys.platform == "darwin" and status_check({"cmd": "status"}) is not None:
            print(f"{label} already running.")
            return
        _launch(service, entry_point, socket_path_fn)
        print(f"Waiting for {label} to finish loading...")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if status_check({"cmd": "status"}) is not None:
                print(f"{label} ready.")
                return
            time.sleep(1)
        print(f"Error: {label} didn't come up within {timeout_s:.0f}s.", file=sys.stderr)
        sys.exit(1)

    _start_and_wait(
        "STT daemon (parakeet-dictate)", STT_SERVICE, STT_ENTRY_POINT, stt_socket_path,
        stt_status, timeout_s=120,
    )
    _start_and_wait(
        "TTS daemon (qwen-tts)", TTS_SERVICE, TTS_ENTRY_POINT, tts_socket_path,
        tts_status, timeout_s=180,
    )
    main()


def main_toggle() -> None:
    """Entry point for the local hotkey (e.g. a numpad key via a GNOME
    custom shortcut): sends a toggle command to the already-running
    `speech-to-speech` process over its Unix socket. Deliberately only
    imports toggle_socket -- no torch/model imports -- so this stays fast
    enough to bind to a hotkey."""
    from speech_to_speech.toggle_socket import send_toggle

    if not send_toggle():
        print(
            "speech-to-speech is not running (start it with `uv run speech-to-speech`)",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
