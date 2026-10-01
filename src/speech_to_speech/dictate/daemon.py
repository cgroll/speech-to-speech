"""Long-running process: loads Parakeet (ONNX, int8, CPU) once, then serves
recording/status commands over a Unix socket. Started and stopped explicitly
via `parakeet-dictate enable` / `disable` (systemd user service) -- never
auto-started and never auto-unloads on idle.

Two clients, two command pairs, same model+mic+worker underneath:
- `dictate/cli.py` (hotkey-facing dictation) uses `toggle_record`, which also
  types the result via `ydotool`.
- `speech_to_speech/stt_client.py` (the voice-chat app) uses
  `start_recording`/`stop_recording`, which return the transcript instead of
  typing it -- see docs/architecture-proposal.md, "Daemon-Aufspaltung" step 4.
"""

import os
import sys

# macOS (Apple Silicon): enable the MPS fallback for ops Metal lacks, and neuter
# autocast when MPS is requested -- both BEFORE torch is imported anywhere.
# nano-parakeet/torch get imported lazily in load_model(), so this guard (run at
# module load) is still first. Mirrors ~/repos/parakeet-dictate's daemon.py.
if sys.platform == "darwin":
    os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
    import torch

    _original_autocast = torch.amp.autocast

    class _CustomAutocast(_original_autocast):
        def __init__(self, device_type, dtype=None, enabled=True, cache_enabled=None):
            if device_type == "mps":
                device_type = "cpu"
                enabled = False
            super().__init__(device_type, dtype=dtype, enabled=enabled, cache_enabled=cache_enabled)

    torch.amp.autocast = _CustomAutocast

import json
import logging
import signal
import socket
import threading
import time

import numpy as np

from speech_to_speech.config import STT_MODEL_NAME_NANO, STT_MODEL_NAME_ONNX
from speech_to_speech.dictate import feedback
from speech_to_speech.dictate.audio import SAMPLE_RATE, Recorder
from speech_to_speech.dictate.keymap import fix_for_de_layout
from speech_to_speech.dictate.protocol import decode_audio
from speech_to_speech.dictate.protocol import socket_path as _socket_path
from speech_to_speech.dictate.typing_backend import TypingError, type_text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class Daemon:
    def __init__(self) -> None:
        self._state = "idle"
        self._recorder = Recorder()
        self._model = None
        self._worker: threading.Thread | None = None
        self._typed: list[str] = []

    def load_model(self) -> None:
        # Same model (Parakeet TDT 0.6b v3), two runtimes: nano-parakeet on the
        # MPS GPU for macOS, onnx_asr (int8, CPU) for Linux. The two expose
        # different load/infer APIs, so the whole method branches -- see
        # _transcribe() for the matching inference split.
        logger.info("Loading STT model (Parakeet)...")
        t0 = time.monotonic()
        if sys.platform == "darwin":
            from nano_parakeet import from_pretrained

            device = "mps" if torch.backends.mps.is_available() else "cpu"
            self._model = from_pretrained(model_name=STT_MODEL_NAME_NANO, device=device)
            logger.info("STT model loaded onto %s in %.1fs", device, time.monotonic() - t0)

            # Warmup: first inference compiles/initialises the MPS kernels.
            self._model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32))
        else:
            import onnx_asr

            self._model = onnx_asr.load_model(STT_MODEL_NAME_ONNX, quantization="int8")
            logger.info("STT model loaded in %.1fs", time.monotonic() - t0)

            # Warmup: first inference triggers one-time onnxruntime session setup.
            self._model.recognize(np.zeros(SAMPLE_RATE, dtype=np.float32), sample_rate=SAMPLE_RATE)
        logger.info("Warmup inference done, ready.")

    def handle_command(self, cmd: dict) -> dict:
        name = cmd.get("cmd")
        if name == "status":
            return {"ok": True, "state": self._state}
        if name == "toggle_record":
            return self._toggle_record()
        # start_recording/stop_recording: the "silent" half of the protocol,
        # used by speech_to_speech.stt_client (the voice-chat app) instead of
        # the hotkey's toggle_record -- same start/stop mechanics, but never
        # types the result via ydotool, since the caller wants the text back
        # to feed to an LLM, not typed into whatever window has focus. See
        # docs/architecture-proposal.md, "Daemon-Aufspaltung" step 4.
        if name == "start_recording":
            return self._start_recording()
        if name == "stop_recording":
            return self._stop_recording()
        # transcribe: bypasses Recorder/VAD entirely -- the caller (Telegram
        # bot, see docs/telegram-bot-proposal.md) already has finished audio
        # (a downloaded, ffmpeg-decoded voice message) and just wants it run
        # through the model once, synchronously, like `status` rather than
        # like a live recording.
        if name == "transcribe":
            return self._transcribe_command(cmd)
        return {"ok": False, "error": f"unknown command: {name}"}

    def _start_recording(self) -> dict:
        if self._state != "idle":
            return {"ok": False, "error": f"busy: {self._state}"}
        self._typed = []
        self._recorder.start()
        self._state = "recording"
        self._worker = threading.Thread(target=self._consume_segments, daemon=True)
        self._worker.start()
        logger.info("Recording started")
        return {"ok": True, "state": "recording"}

    def _stop_recording(self) -> dict:
        if self._state != "recording":
            return {"ok": False, "error": f"not recording: {self._state}"}
        self._recorder.stop()
        self._state = "transcribing"
        logger.info("Recording stopped, draining remaining segments")
        try:
            assert self._worker is not None
            self._worker.join()
        finally:
            self._state = "idle"
        text = " ".join(self._typed).strip()
        return {"ok": True, "text": text}

    def _transcribe_command(self, cmd: dict) -> dict:
        # Same busy-guard as start_recording -- one desktop user, one mic,
        # one model instance, so a transcribe request while a live
        # recording is in progress is refused rather than queued.
        if self._state != "idle":
            return {"ok": False, "error": f"busy: {self._state}"}
        try:
            audio = decode_audio(cmd["audio"])
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": f"invalid audio payload: {exc}"}
        return {"ok": True, "text": self._transcribe(audio)}

    def _toggle_record(self) -> dict:
        if self._state == "idle":
            result = self._start_recording()
            if result["ok"]:
                feedback.play_cue("start")
            return result

        if self._state == "recording":
            feedback.play_cue("stop")
            result = self._stop_recording()
            if not result["ok"]:
                return result
            text = result["text"]
            if not text:
                feedback.notify("No speech detected")
                return result
            try:
                # macOS pastes via the clipboard (typing_backend), which is
                # layout-independent -- umlauts come through natively, so the
                # QWERTZ keymap fix is Linux/ydotool-only.
                typed = text if sys.platform == "darwin" else fix_for_de_layout(text)
                type_text(typed)
            except TypingError as exc:
                logger.error("Typing failed: %s", exc)
                feedback.notify(f"Typing failed: {exc}", error=True)
            return result

        # transcribing: shouldn't be reachable since we handle one
        # connection at a time, but guard anyway.
        return {"ok": False, "error": f"busy: {self._state}"}

    def _consume_segments(self) -> None:
        """Runs in a background thread while recording: transcribes each
        speech segment as soon as a pause cuts it, so inference happens
        alongside capture instead of after recording stops. Text is only
        accumulated here -- nothing is typed until the recording is stopped.
        Drains to completion (including the final segment stop() flushes)
        once the recorder's queue closes."""
        for audio in self._recorder.segments():
            text = self._transcribe(audio)
            if text:
                self._typed.append(text)

    def _transcribe(self, audio: np.ndarray) -> str:
        if len(audio) < SAMPLE_RATE // 4:  # less than 250ms, not worth transcribing
            return ""

        if sys.platform == "darwin":
            # nano-parakeet assumes 16kHz mono float32 (our SAMPLE_RATE) and
            # takes no sample_rate arg.
            return self._model.transcribe(audio).strip()
        return self._model.recognize(audio, sample_rate=SAMPLE_RATE).strip()


def main() -> None:
    daemon = Daemon()
    daemon.load_model()

    path = _socket_path()
    if os.path.exists(path):
        os.unlink(path)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
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
            with conn:
                try:
                    # A single recv(4096) used to be enough -- every command
                    # was a tiny JSON object. The `transcribe` command's
                    # audio payload (base64, easily >100KB for a few
                    # seconds) no longer fits in one read, so loop until the
                    # client's sock.shutdown(SHUT_WR) signals EOF (see
                    # protocol.py's send_command), same pattern already used
                    # below for reading the response back on the client
                    # side.
                    chunks = []
                    while True:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    if not chunks:
                        continue
                    cmd = json.loads(b"".join(chunks).decode())
                    response = daemon.handle_command(cmd)
                except Exception as exc:  # noqa: BLE001 - report to client, keep serving
                    logger.exception("Error handling command")
                    response = {"ok": False, "error": str(exc)}
                conn.sendall((json.dumps(response) + "\n").encode())
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
