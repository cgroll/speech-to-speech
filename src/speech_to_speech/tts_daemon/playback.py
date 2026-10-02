"""Server-side audio playback for the shared TTS daemon: plays synthesized
chunks directly on this machine's speakers, in-process, right where they're
generated -- see docs/architecture-proposal.md, "Daemon-Aufspaltung" step 5.
Moved here from the app process's audio_io.py without behavior changes."""

import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator

import numpy as np
import sounddevice as sd

from speech_to_speech.config import JABRA_DEVICE_NAME, TTS_PLAYBACK_SPEED

logger = logging.getLogger(__name__)


def _output_device() -> int | None:
    """Looks up the Jabra by name among output-capable devices, rather than
    relying on sounddevice's default output -- that default tracks macOS'
    system output setting, which stays on the Mac speakers even while the
    Jabra is the default *input* (mic), so TTS replies would otherwise come
    out of the laptop instead of the headset. Falls back to the default
    device (None) if the Jabra isn't connected, same graceful-degradation
    pattern as the Jabra button (see input_button.py).

    macOS-only: on Linux the name match hits the raw ALSA device (hw:N,0),
    which doesn't resample and rejects the TTS model's 24 kHz output
    ("Invalid sample rate"). There the PipeWire/pulse default already routes
    to the headset, so stick with it."""
    if sys.platform != "darwin":
        return None
    for i, info in enumerate(sd.query_devices()):
        if JABRA_DEVICE_NAME in info["name"] and info["max_output_channels"] > 0:
            return i
    logger.warning("No '%s' output device found, falling back to the system default.", JABRA_DEVICE_NAME)
    return None


def play_audio_streaming(
    chunks: Iterator[tuple[np.ndarray, int]],
    stop_event: threading.Event | None = None,
    on_first_chunk: Callable[[], None] | None = None,
    on_chunk: Callable[[], None] | None = None,
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

    `on_first_chunk`, if given, fires once, exactly when the first sample is
    written to the output stream -- i.e. after ffmpeg's atempo stretch when
    TTS_PLAYBACK_SPEED != 1.0, not just once the first raw TTS chunk exists.
    Used to time "time to first speech" for the caller.

    `on_chunk`, if given, fires every time a chunk is pulled from `chunks`
    (i.e. as soon as the GPU has produced it, before playback/time-stretch) --
    a progress heartbeat the caller's stall watchdog uses to tell "still
    generating, just a long reply" apart from "stuck mid-chunk" (see
    daemon.py's `_run_with_watchdog`).
    """
    t0 = time.monotonic()
    device = _output_device()
    it = iter(chunks)
    try:
        first_chunk, sample_rate = next(it)
    except StopIteration:
        return
    if on_chunk is not None:
        on_chunk()

    def _all() -> Iterator[np.ndarray]:
        yield first_chunk
        while stop_event is None or not stop_event.is_set():
            try:
                chunk, _ = next(it)
            except StopIteration:
                return
            if on_chunk is not None:
                on_chunk()
            yield chunk

    if TTS_PLAYBACK_SPEED == 1.0:
        with sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32", device=device) as stream:
            first = True
            for chunk in _all():
                if first and on_first_chunk is not None:
                    on_first_chunk()
                    first = False
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
    with sd.OutputStream(samplerate=sample_rate, channels=1, dtype="float32", device=device) as stream:
        first = True
        while data := os.read(stdout_fd, 4096):
            if first and on_first_chunk is not None:
                on_first_chunk()
                first = False
            stream.write(np.frombuffer(data, dtype=np.float32).reshape(-1, 1))
            if stop_event is not None and stop_event.is_set():
                break

    if stop_event is not None and stop_event.is_set():
        proc.terminate()  # don't wait for ffmpeg to drain its input on its own

    feeder.join()
    logger.info("Streaming playback finished in %.1fs", time.monotonic() - t0)
