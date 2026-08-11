"""Text-to-speech via Qwen3-TTS CustomVoice (GPU). Fixed speaker embedding
(no reference audio needed), same API used in qwen3-tts-local's
test_generate.py."""

import logging
import time

import numpy as np
import torch

from speech_to_speech.config import TTS_LANGUAGE, TTS_MODEL_ID, TTS_SPEAKER

logger = logging.getLogger(__name__)


class TextToSpeech:
    def __init__(self) -> None:
        self._model = None

    def load(self) -> None:
        from qwen_tts import Qwen3TTSModel

        logger.info("Loading TTS model (Qwen3-TTS %s)...", TTS_MODEL_ID)
        t0 = time.monotonic()
        self._model = Qwen3TTSModel.from_pretrained(
            TTS_MODEL_ID,
            device_map="cuda:0",
            dtype=torch.bfloat16,
            attn_implementation="sdpa",
        )
        logger.info("TTS model loaded in %.1fs", time.monotonic() - t0)

        # Warmup: first generation pays for CUDA/cuDNN kernel autotuning:
        # eat that cost here instead of on the first real reply.
        t0 = time.monotonic()
        self.synthesize("Warmup.")
        logger.info("TTS warmup done in %.1fs, ready.", time.monotonic() - t0)

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        t0 = time.monotonic()
        wavs, sample_rate = self._model.generate_custom_voice(
            text=text,
            language=TTS_LANGUAGE,
            speaker=TTS_SPEAKER,
        )
        wav = wavs[0]
        duration_s = len(wav) / sample_rate
        elapsed = time.monotonic() - t0
        logger.info(
            "TTS generated %.1fs of audio in %.1fs (%.1fx realtime)",
            duration_s,
            elapsed,
            duration_s / elapsed if elapsed else 0.0,
        )
        return wav, sample_rate
