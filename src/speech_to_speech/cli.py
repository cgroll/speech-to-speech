import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)


def main() -> None:
    # Imported lazily so `main_toggle` (bound to a hotkey, called often)
    # doesn't pay for torch/model-related imports it never needs.
    from dotenv import load_dotenv

    from speech_to_speech.app import App

    load_dotenv()
    app = App()
    try:
        app.load()
        app.run()
    except KeyboardInterrupt:
        print("\nBye.")
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


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
