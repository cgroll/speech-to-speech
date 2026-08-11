"""Local Unix-socket toggle trigger, so the same record-toggle action can be
bound to a GNOME custom keyboard shortcut (e.g. a numpad key) in addition to
the Jabra button -- for when you're at the desk anyway.

Deliberately not implemented via evdev on the main keyboard: reading a
keyboard's raw event stream to catch one key means the process *can* see
every keystroke on that device, including passwords (see input_button.py's
docstring on why the Jabra button gets a udev rule scoped to just that one
USB device, not the general 'input' group). GNOME's shortcut system hands
this process only the one bound key, via a lightweight CLI call over this
socket -- same shape as parakeet-dictate's daemon/CLI split.
"""

import logging
import os
import socket
from typing import Callable

logger = logging.getLogger(__name__)


def _socket_path() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    return os.path.join(runtime_dir, "speech-to-speech.sock")


def serve(on_toggle: Callable[[], None]) -> None:
    """Blocks forever, calling on_toggle() for each 'toggle' command received.

    Acks the client *before* calling on_toggle(), not after: on_toggle() can
    take many seconds (recording stop -> LLM -> TTS -> playback all happen
    synchronously inside it), far longer than a client should have to block
    just to confirm the command was received. Acking first also means a
    slow on_toggle() can never hit a client that already gave up and closed
    its end (which previously crashed this whole thread with a
    BrokenPipeError on the late write -- one bad connection took down the
    socket server for the rest of the process's life). Each connection is
    now also wrapped in its own try/except so a future per-connection error
    can't do that again.
    """
    path = _socket_path()
    if os.path.exists(path):
        os.unlink(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    logger.info("Toggle socket listening on %s", path)
    try:
        while True:
            conn, _ = server.accept()
            try:
                with conn:
                    data = conn.recv(4096)
                    conn.sendall(b"ok\n")
                if data.decode().strip() == "toggle":
                    on_toggle()
            except OSError:
                logger.warning("Toggle socket connection error", exc_info=True)
    finally:
        server.close()
        if os.path.exists(path):
            os.unlink(path)


def send_toggle(timeout: float = 5.0) -> bool:
    """Client side, called by the `speech-to-speech-toggle` command bound to
    a hotkey. Returns False if the app isn't running (no socket to connect
    to) instead of raising, so the hotkey command can fail quietly."""
    path = _socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall(b"toggle")
            sock.shutdown(socket.SHUT_WR)
            sock.recv(4096)
        return True
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        return False
