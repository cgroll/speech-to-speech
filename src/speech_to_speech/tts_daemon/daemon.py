"""Long-running process: loads the Qwen3-TTS model once, then serves speak/
stop/status commands over a Unix socket. Started/stopped explicitly via
`qwen-tts enable`/`disable` (systemd user service) -- never auto-started,
same control model as the STT daemon (speech_to_speech/dictate/daemon.py).

Unlike the STT daemon's commands, which all return quickly, `speak` blocks
for the whole duration of playback (which happens server-side, on this
machine's speakers -- see docs/architecture-proposal.md, "Daemon-Aufspaltung"
step 5). A barge-in's `stop` command must be able to reach the daemon while
a `speak` call is still in flight on another connection, so -- unlike the STT
daemon's single-threaded accept loop -- each connection here is handled on
its own thread.

`stop` only sets an in-process `threading.Event`, checked between TTS chunks
(see tts_daemon/playback.py) -- it can't interrupt a single chunk's
generation call once it's already running on the GPU. Normally that's well
under a second, but some inputs make the model run long past a natural stop
without yielding, and a blocking GPU call can't be cancelled from another
thread in Python, only the process it runs in can be killed. `_speak()`
therefore runs playback under a watchdog: if it's still going past
`_SPEAK_WATCHDOG_S`, the daemon kills itself (`os._exit`) instead of staying
wedged in `speaking` forever and rejecting every request after it. systemd's
`Restart=on-failure` (see systemd/qwen-tts.service) brings up a fresh
process, which costs a model reload (~10s) but bounds the outage instead of
requiring a manual restart.
"""

import json
import logging
import os
import signal
import socket
import sys
import threading
import time

import numpy as np

from speech_to_speech.dictate.protocol import encode_audio
from speech_to_speech.tts_daemon import playback
from speech_to_speech.tts_daemon.protocol import socket_path as _socket_path
from speech_to_speech.tts_daemon.tts import TextToSpeech

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Generous vs. a normal turn (a few seconds, per the app's "short, natural
# sentences" system prompt) but well under TTS_MAX_NEW_TOKENS's multi-minute
# worst case -- long enough that it won't fire on a legitimately long reply,
# short enough to bound how long the daemon stays unusable if one does hang.
_SPEAK_WATCHDOG_S = 90.0


class Daemon:
    def __init__(self) -> None:
        self._state = "idle"
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._tts = TextToSpeech()

    def load_model(self) -> None:
        self._tts.load()

    def handle_command(self, cmd: dict) -> dict:
        name = cmd.get("cmd")
        if name == "status":
            return {"ok": True, "state": self._state}
        if name == "speak":
            return self._speak(cmd.get("text", ""))
        # synthesize: same generation as speak(), but returns the audio as
        # base64 bytes instead of playing it on this machine's speakers --
        # for remote callers (e.g. the Gradio cockpit reached over Tailscale
        # from a phone) that need to hand the audio to a browser player
        # instead of local playback. See docs/architecture-proposal.md,
        # "Offene Frage: mobiler Zugriff (Handy)".
        if name == "synthesize":
            return self._synthesize(cmd.get("text", ""))
        if name == "stop":
            return self._stop()
        return {"ok": False, "error": f"unknown command: {name}"}

    def _claim_speaking(self) -> threading.Event | dict:
        """Shared busy-guard + state-claim for speak()/synthesize(): both
        drive the same GPU model and can't run concurrently with each other
        or with themselves. Returns the fresh stop_event on success, or an
        error response dict if the daemon wasn't idle."""
        with self._state_lock:
            if self._state != "idle":
                return {"ok": False, "error": f"busy: {self._state}"}
            self._state = "speaking"
            self._stop_event = threading.Event()
        return self._stop_event

    def _run_with_watchdog(self, target) -> None:
        """Runs `target` in a background thread and waits up to
        `_SPEAK_WATCHDOG_S`. If it's still running past that, kills the
        daemon process (`os._exit`) instead of staying wedged in `speaking`
        forever and rejecting every request after it -- see the module
        docstring and systemd/qwen-tts.service's `Restart=on-failure`. Shared
        by speak() and synthesize(), which only differ in what they do with
        each generated chunk."""
        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        worker.join(timeout=_SPEAK_WATCHDOG_S)

        if worker.is_alive():
            logger.error(
                "TTS call still running after %.0fs with no natural stop -- "
                "can't cancel a blocking GPU call from another thread, "
                "restarting the daemon process instead.",
                _SPEAK_WATCHDOG_S,
            )
            os._exit(1)

    def _speak(self, text: str) -> dict:
        stop_event = self._claim_speaking()
        if isinstance(stop_event, dict):
            return stop_event

        t0 = time.monotonic()
        first_chunk_s = None
        error: list[Exception] = []

        def _on_first_chunk() -> None:
            nonlocal first_chunk_s
            first_chunk_s = time.monotonic() - t0

        def _run() -> None:
            try:
                playback.play_audio_streaming(
                    self._tts.synthesize_streaming(text),
                    stop_event=stop_event,
                    on_first_chunk=_on_first_chunk,
                )
            except Exception as exc:  # noqa: BLE001 - reported to the client below
                error.append(exc)

        self._run_with_watchdog(_run)

        with self._state_lock:
            self._state = "idle"

        if error:
            return {"ok": False, "error": str(error[0])}

        return {
            "ok": True,
            "state": "cancelled" if stop_event.is_set() else "done",
            "first_chunk_s": first_chunk_s,
        }

    def _synthesize(self, text: str) -> dict:
        stop_event = self._claim_speaking()
        if isinstance(stop_event, dict):
            return stop_event

        t0 = time.monotonic()
        first_chunk_s = None
        error: list[Exception] = []
        chunks: list[np.ndarray] = []
        sample_rate = 0

        def _run() -> None:
            nonlocal first_chunk_s, sample_rate
            try:
                for chunk, sr in self._tts.synthesize_streaming(text):
                    if stop_event.is_set():
                        break
                    if first_chunk_s is None:
                        first_chunk_s = time.monotonic() - t0
                    sample_rate = sr
                    chunks.append(chunk)
            except Exception as exc:  # noqa: BLE001 - reported to the client below
                error.append(exc)

        self._run_with_watchdog(_run)

        with self._state_lock:
            self._state = "idle"

        if error:
            return {"ok": False, "error": str(error[0])}

        # Concatenated once at the end rather than streamed back chunk by
        # chunk -- fine for this first, simple (non-streaming) remote path;
        # see docs/architecture-proposal.md for the later streaming option.
        audio = np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, dtype=np.float32)
        return {
            "ok": True,
            "state": "cancelled" if stop_event.is_set() else "done",
            "audio": encode_audio(audio),
            "sample_rate": sample_rate,
            "first_chunk_s": first_chunk_s,
        }

    def _stop(self) -> dict:
        with self._state_lock:
            if self._state != "speaking":
                return {"ok": True, "state": self._state}
            self._stop_event.set()
        return {"ok": True, "state": "stopping"}


def _handle_conn(conn: socket.socket, daemon: Daemon) -> None:
    with conn:
        try:
            chunks = []
            while data := conn.recv(4096):
                chunks.append(data)
            if not chunks:
                return
            cmd = json.loads(b"".join(chunks).decode())
            response = daemon.handle_command(cmd)
        except Exception as exc:  # noqa: BLE001 - report to client, keep serving
            logger.exception("Error handling command")
            response = {"ok": False, "error": str(exc)}
        conn.sendall((json.dumps(response) + "\n").encode())


def main() -> None:
    daemon = Daemon()
    daemon.load_model()

    path = _socket_path()
    if os.path.exists(path):
        os.unlink(path)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(5)
    logger.info("Listening on %s", path)

    def _cleanup(*_args) -> None:
        logger.info("Shutting down")
        server.close()
        if os.path.exists(path):
            os.unlink(path)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    try:
        while True:
            conn, _ = server.accept()
            threading.Thread(target=_handle_conn, args=(conn, daemon), daemon=True).start()
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
