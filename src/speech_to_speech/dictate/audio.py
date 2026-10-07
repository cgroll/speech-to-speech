"""Microphone capture, open only for the duration of a single recording.

While recording, incoming audio is run through a VAD frame-by-frame. Once a
pause follows a stretch of speech, that stretch is cut into a segment and
handed off on a queue -- the caller can transcribe/type it immediately while
capture keeps going, instead of waiting for the whole recording to end.
"""

import logging
import queue
import threading
from collections import deque
from typing import Iterator

import numpy as np
import sounddevice as sd
import webrtcvad

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000

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
        self._stream = self._open_stream()
        self._stream.start()

    def _open_stream(self) -> sd.InputStream:
        try:
            return self._new_stream()
        except sd.PortAudioError:
            # PortAudio snapshots the device list once, at Pa_Initialize()
            # time, which for this long-lived daemon process means once at
            # daemon startup (it's never restarted on idle -- see
            # daemon.py's docstring). If the Jabra headset drops and
            # re-pairs later (or macOS otherwise reshuffles CoreAudio
            # devices), that snapshot goes stale and opening a stream fails
            # with an opaque "Internal PortAudio error" [-9986] even though
            # the device works fine -- a freshly started process sees it
            # without issue. This is also why the cockpit's "STT-Daemon neu
            # starten" button "fixes" it: it forces a fresh PortAudio init,
            # not anything audio-specific. Doing that reinit here instead
            # (and retrying once) means a stale device list heals itself
            # without needing a manual restart.
            logger.warning(
                "Opening input stream failed, likely a stale PortAudio "
                "device list (e.g. after a Bluetooth reconnect); "
                "reinitializing PortAudio and retrying once."
            )
            sd._terminate()
            sd._initialize()
            return self._new_stream()

    def _new_stream(self) -> sd.InputStream:
        return sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=_FRAME_SAMPLES,
            callback=self._on_audio,
        )

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
