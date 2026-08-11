"""Speech-to-text via Parakeet (ONNX, int8, CPU) -- same model and loading
pattern as parakeet-dictate's daemon.py, no GPU usage so there's no
contention with the TTS model."""

import logging
import time

import numpy as np

from speech_to_speech.config import STT_MODEL_NAME, STT_SAMPLE_RATE

logger = logging.getLogger(__name__)


class SpeechToText:
    def __init__(self) -> None:
        self._model = None

    def load(self) -> None:
        import onnx_asr

        logger.info("Loading STT model (Parakeet)...")
        t0 = time.monotonic()
        self._model = onnx_asr.load_model(STT_MODEL_NAME, quantization="int8")
        logger.info("STT model loaded in %.1fs", time.monotonic() - t0)

        # Warmup: first inference triggers one-time onnxruntime session setup.
        self._model.recognize(
            np.zeros(STT_SAMPLE_RATE, dtype=np.float32), sample_rate=STT_SAMPLE_RATE
        )
        logger.info("STT warmup done, ready.")

    def transcribe(self, audio: np.ndarray) -> str:
        if len(audio) < STT_SAMPLE_RATE // 4:  # less than 250ms, not worth transcribing
            return ""
        return self._model.recognize(audio, sample_rate=STT_SAMPLE_RATE).strip()
