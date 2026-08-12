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
    """Convenience entry point: starts both shared daemons via systemctl
    (idempotent -- a no-op if one is already running) and waits until each
    is actually reachable over its socket, not just until `systemctl start`
    returns -- model loading takes a few seconds to a couple minutes after
    that. Then launches the app. One command instead of the usual three
    (`parakeet-dictate enable`, `qwen-tts enable`, `speech-to-speech`)."""
    import subprocess
    import time

    from speech_to_speech.dictate.cli import SERVICE_NAME as STT_SERVICE
    from speech_to_speech.dictate.protocol import send_command as stt_status
    from speech_to_speech.tts_daemon.cli import SERVICE_NAME as TTS_SERVICE
    from speech_to_speech.tts_daemon.protocol import send_command as tts_status

    def _start_and_wait(label: str, service: str, status_check, timeout_s: float) -> None:
        subprocess.run(["systemctl", "--user", "start", service], check=True)
        print(f"Waiting for {label} to finish loading...")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if status_check({"cmd": "status"}) is not None:
                print(f"{label} ready.")
                return
            time.sleep(1)
        print(f"Error: {label} didn't come up within {timeout_s:.0f}s.", file=sys.stderr)
        sys.exit(1)

    _start_and_wait("STT daemon (parakeet-dictate)", STT_SERVICE, stt_status, timeout_s=60)
    _start_and_wait("TTS daemon (qwen-tts)", TTS_SERVICE, tts_status, timeout_s=120)
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
