"""Shared Unix-socket JSON-command plumbing for the TTS daemon (`daemon.py`)
and its client (`speech_to_speech/tts_client.py`). Same request/response
framing as the STT daemon's `dictate/protocol.py`, but its own socket --
each daemon owns its own model and doesn't need to know the other exists.
"""

import json
import os
import socket

SOCKET_NAME = "qwen-tts.sock"


def socket_path() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    return os.path.join(runtime_dir, SOCKET_NAME)


def send_command(cmd: dict, timeout: float = 30.0) -> dict | None:
    """Sends one JSON command over a fresh connection and returns the
    decoded JSON response, or None if the daemon isn't reachable (not
    running, a stale socket file, or -- for `speak`, whose watchdog can kill
    the process mid-response, see daemon.py -- a connection that dropped
    before finishing)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(socket_path())
            sock.sendall(json.dumps(cmd).encode())
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            return None
        return json.loads(data.decode())
    except (
        FileNotFoundError,
        ConnectionRefusedError,
        ConnectionResetError,
        BrokenPipeError,
        json.JSONDecodeError,
    ):
        return None
