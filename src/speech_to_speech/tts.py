"""Text-to-speech via Qwen3-TTS CustomVoice (GGML GPU). Fixed speaker
embedding, using faster-qwen3-tts with GGML quantization. Audio is yielded
in chunks as soon as they are ready so playback can start immediately."""

import logging
import time
from collections.abc import Iterator

import numpy as np

from speech_to_speech.config import TTS_LANGUAGE, TTS_MODEL_ID, TTS_SPEAKER

logger = logging.getLogger(__name__)


class TextToSpeech:
    def __init__(self) -> None:
        self._model = None

    def load(self) -> None:
        from faster_qwen3_tts import FasterQwen3TTS

        logger.info("Loading TTS model (Qwen3-TTS %s, GGML)...", TTS_MODEL_ID)
        t0 = time.monotonic()
        self._model = FasterQwen3TTS.from_pretrained(TTS_MODEL_ID)
        logger.info("TTS model loaded in %.1fs", time.monotonic() - t0)

        t0 = time.monotonic()
        self._model.warmup()
        logger.info("TTS warmup done in %.1fs, ready.", time.monotonic() - t0)

    def synthesize_streaming(self, text: str) -> Iterator[tuple[np.ndarray, int]]:
        t0 = time.monotonic()
        first = True
        for audio_chunk, sr, _timing in self._model.generate_custom_voice_streaming(
            text=text,
            language=TTS_LANGUAGE,
            speaker=TTS_SPEAKER,
            chunk_size=8,
        ):
            if first:
                logger.info("TTS first chunk in %.2fs", time.monotonic() - t0)
                first = False
            yield audio_chunk, sr
        logger.info("TTS generation done in %.1fs", time.monotonic() - t0)
