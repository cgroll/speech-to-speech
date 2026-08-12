"""Shared Unix-socket JSON-command plumbing for the STT daemon
(`daemon.py`) and its two clients: the hotkey-facing dictation CLI
(`cli.py`, typed dictation via `toggle_record`) and the voice-chat app's
silent client (`speech_to_speech/stt_client.py`, `start_recording` /
`stop_recording`, see docs/architecture-proposal.md "Daemon-Aufspaltung"
step 4). Factored out so both clients agree on the same socket path and
request/response framing instead of each re-implementing it.
"""

import json
import os
import socket

SOCKET_NAME = "parakeet-dictate.sock"


def socket_path() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    return os.path.join(runtime_dir, SOCKET_NAME)


def send_command(cmd: dict, timeout: float = 30.0) -> dict | None:
    """Sends one JSON command over a fresh connection and returns the
    decoded JSON response, or None if the daemon isn't reachable (not
    running, or a stale socket file)."""
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
        return json.loads(b"".join(chunks).decode())
    except (FileNotFoundError, ConnectionRefusedError):
        return None
