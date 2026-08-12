"""Microphone capture and playback.

`Recorder` is adapted from parakeet-dictate's `audio.py`: opens the mic only
for the duration of a recording, runs a VAD frame-by-frame, and cuts speech
into segments on a queue as soon as a pause follows them, so transcription
can happen alongside capture instead of after recording stops.
"""

import logging
import os
import queue
import subprocess
import threading
import time
from collections import deque
from collections.abc import Iterator

import numpy as np
import sounddevice as sd
import webrtcvad

from speech_to_speech.config import STT_SAMPLE_RATE, TTS_PLAYBACK_SPEED

logger = logging.getLogger(__name__)

SAMPLE_RATE = STT_SAMPLE_RATE

_FRAME_MS = 30
_FRAME_SAMPLES = SAMPLE_RATE * _FRAME_MS // 1000  # 480
_PAUSE_MS = 2000  # silence after speech needed to cut a segment
_MIN_SPEECH_MS = 6_000  # require this much speech before a pause cuts a segment
_PREROLL_MS = 300  # audio kept before speech onset, so segments don't clip attacks
_PREROLL_FRAMES = _PREROLL_MS // _FRAME_MS
_VAD_MODE = 2  # 0-3, higher = more aggressive about filtering out non-speech


class Recorder:
    """Opens an input stream on start(), buffers float32 mono audio, and
    closes the stream again on stop() -- the mic is never open outside of
    an active recording.

    Speech segments (cut on VAD-detected pauses) are pushed to a queue as
    soon as they're ready; consume them via segments(). stop() flushes
    whatever's left as a final segment and signals end-of-stream.
    """

    def __init__(self) -> None:
        self._stream: sd.InputStream | None = None
        self._vad = webrtcvad.Vad(_VAD_MODE)
        self._lock = threading.Lock()
        self._segment_queue: queue.Queue[np.ndarray | None] = queue.Queue()
        self._preroll: deque[np.ndarray] = deque(maxlen=_PREROLL_FRAMES)
        self._current: list[np.ndarray] = []
        self._has_speech = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0

    def start(self) -> None:
        if self._stream is not None:
            return
        self._segment_queue = queue.Queue()
        self._preroll.clear()
        self._current = []
        self._has_speech = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=_FRAME_SAMPLES,
            callback=self._on_audio,
        )
        self._stream.start()

    def _on_audio(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            logger.warning("Recorder input status: %s", status)
        if frames != _FRAME_SAMPLES:
            return  # partial block (e.g. right before stop()); negligible to drop
        frame = indata.copy().reshape(-1)
        is_speech = self._vad.is_speech(_to_pcm16(frame), SAMPLE_RATE)
        with self._lock:
            if is_speech:
                if not self._has_speech:
                    self._current.extend(self._preroll)
                    self._preroll.clear()
                    self._has_speech = True
                self._current.append(frame)
                self._speech_ms += _FRAME_MS
                self._silence_ms = 0.0
            elif self._has_speech:
                self._current.append(frame)
                self._silence_ms += _FRAME_MS
                if self._silence_ms >= _PAUSE_MS and self._speech_ms >= _MIN_SPEECH_MS:
                    self._flush_locked()
            else:
                self._preroll.append(frame)

    def _flush_locked(self, *, final: bool = False) -> None:
        """Cuts the current segment to the queue. Caller holds self._lock."""
        if self._current:
            self._segment_queue.put(np.concatenate(self._current))
        self._current = []
        self._has_speech = False
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        if final:
            self._segment_queue.put(None)  # sentinel: end of recording

    def stop(self) -> None:
        """Stops and closes the stream, flushing any buffered audio as a
        final segment and signalling end-of-stream to segments()."""
        if self._stream is None:
            return
        self._stream.stop()
        self._stream.close()
        self._stream = None
        with self._lock:
            self._flush_locked(final=True)

    def segments(self) -> Iterator[np.ndarray]:
        """Yields speech segments as they're cut, until stop() is called."""
        while True:
            item = self._segment_queue.get()
            if item is None:
                return
            yield item


def _to_pcm16(frame: np.ndarray) -> bytes:
    return np.clip(frame * 32768.0, -32768, 32767).astype(np.int16).tobytes()


def _time_stretch(samples: np.ndarray, sample_rate: int, rate: float) -> np.ndarray:
    """Pitch-preserving time-stretch via ffmpeg's `atempo` filter (WSOLA-based).
    Librosa's `effects.time_stretch` (phase vocoder) was tried first but gives
    speech a tinny/robotic sound at this stretch factor -- atempo sounds
    natural, same trick players use for >1x playback speed. atempo only
    accepts rates in [0.5, 2.0]; TTS_PLAYBACK_SPEED is expected to stay in
    that range for a PoC."""
    proc = subprocess.run(
        [
            "ffmpeg", "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
            "-filter:a", f"atempo={rate}",
            "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "pipe:1",
        ],
        input=samples.astype(np.float32).tobytes(),
        capture_output=True,
        check=True,
    )
    return np.frombuffer(proc.stdout, dtype=np.float32)


def play_audio(samples: np.ndarray, sample_rate: int) -> None:
    """Blocking playback with pitch-preserving time-stretch."""
    if TTS_PLAYBACK_SPEED != 1.0:
        samples = _time_stretch(samples, sample_rate, TTS_PLAYBACK_SPEED)
    t0 = time.monotonic()
    sd.play(samples, sample_rate)
    sd.wait()
    logger.info("Playback finished in %.1fs", time.monotonic() - t0)


def play_audio_streaming(
    chunks: Iterator[tuple[np.ndarray, int]],
    stop_event: threading.Event | None = None,
) -> None:
    """Stream TTS audio chunks to playback with pitch-preserving time-stretch.

    Feeds each chunk into a persistent ffmpeg atempo process as it arrives and
    plays the stretched output immediately, so the first sound is heard as soon
    as the first TTS chunk is ready (~160ms with GGML) rather than after the
    full synthesis completes.

    If `stop_event` is set (e.g. on barge-in), playback stops within about one
    chunk's worth of audio, and no further chunks are pulled from `chunks`.
    Since `tts.py`'s generator only produces the next chunk when asked, that
    also halts the underlying GPU generation, not just the already-produced
    audio -- no separate cancellation path into the TTS model is needed.
    """
    t0 = time.monotonic()
    it = iter(chunks)
    try:
        first_chunk, sample_rate = next(it)
    except StopIteration:
        return

    def _all() -> Iterator[np.ndarray]:
        yield first_chunk
        while stop_event is None or not stop_event.is_set():
            try:
                chunk, _ = next(it)
            except StopIteration:
                return
            yield chunk

    if TTS_PLAYBACK_SPEED == 1.0:
        with sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32") as stream:
            for chunk in _all():
                stream.write(chunk.astype(np.float32).reshape(-1, 1))
        logger.info("Streaming playback finished in %.1fs", time.monotonic() - t0)
        return

    proc = subprocess.Popen(
        [
            "ffmpeg",
            "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
            "-filter:a", f"atempo={TTS_PLAYBACK_SPEED}",
            "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "pipe:1",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )

    def _feed() -> None:
        try:
            for chunk in _all():
                proc.stdin.write(chunk.astype(np.float32).tobytes())
        except BrokenPipeError:
            pass  # ffmpeg already gone (e.g. terminated after a barge-in)
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    feeder = threading.Thread(target=_feed, daemon=True)
    feeder.start()

    stdout_fd = proc.stdout.fileno()
    with sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32") as stream:
        while data := os.read(stdout_fd, 4096):
            stream.write(np.frombuffer(data, dtype=np.float32).reshape(-1, 1))
            if stop_event is not None and stop_event.is_set():
                break

    if stop_event is not None and stop_event.is_set():
        proc.terminate()  # don't wait for ffmpeg to drain its input on its own

    feeder.join()
    logger.info("Streaming playback finished in %.1fs", time.monotonic() - t0)
