"""Text-to-speech via Qwen3-TTS CustomVoice (GGML GPU). Fixed speaker
embedding, using faster-qwen3-tts with GGML quantization. Audio is yielded
in chunks as soon as they are ready so playback can start immediately."""

import logging
import sys
import time
from collections.abc import Iterator

import numpy as np

from speech_to_speech.config import (
    TTS_GGUF_DIR,
    TTS_GGUF_TALKER,
    TTS_GGUF_TOKENIZER,
    TTS_LANGUAGE,
    TTS_MAX_NEW_TOKENS,
    TTS_MAX_SEQ_LEN,
    TTS_MODEL_ID,
    TTS_SPEAKER,
)

logger = logging.getLogger(__name__)


class TextToSpeech:
    def __init__(self) -> None:
        self._model = None

    def load(self) -> None:
        from faster_qwen3_tts import FasterQwen3TTS

        # Two runtimes for the same model: Linux/CUDA uses the "torch" backend
        # with captured CUDA graphs (max_seq_len sizes the static KV cache);
        # macOS runs the qwentts.cpp "ggml" backend on Metal (the torch path
        # errors out with "CUDA graphs require CUDA device"). The ggml backend
        # manages its own cache, so max_seq_len doesn't apply there.
        t0 = time.monotonic()
        if sys.platform == "darwin":
            talker = TTS_GGUF_DIR / TTS_GGUF_TALKER
            tokenizer = TTS_GGUF_DIR / TTS_GGUF_TOKENIZER
            if talker.exists() and tokenizer.exists():
                logger.info(
                    "Loading TTS model (Qwen3-TTS %s, GGML/Metal) from local GGUF in %s...",
                    TTS_MODEL_ID,
                    TTS_GGUF_DIR,
                )
                self._model = FasterQwen3TTS.from_pretrained(
                    TTS_MODEL_ID,
                    backend="ggml",
                    gguf_talker_path=str(talker),
                    gguf_codec_path=str(tokenizer),
                )
            else:
                logger.info("Loading TTS model (Qwen3-TTS %s, GGML/Metal) from HF...", TTS_MODEL_ID)
                self._model = FasterQwen3TTS.from_pretrained(TTS_MODEL_ID, backend="ggml")
        else:
            logger.info("Loading TTS model (Qwen3-TTS %s, torch/CUDA)...", TTS_MODEL_ID)
            self._model = FasterQwen3TTS.from_pretrained(TTS_MODEL_ID, max_seq_len=TTS_MAX_SEQ_LEN)
        logger.info("TTS model loaded in %.1fs", time.monotonic() - t0)

        t0 = time.monotonic()
        self._model.warmup()
        logger.info("TTS warmup done in %.1fs, ready.", time.monotonic() - t0)

    def synthesize_streaming(self, text: str) -> Iterator[tuple[np.ndarray, int]]:
        t0 = time.monotonic()
        first = True
        total_steps = 0
        for audio_chunk, sr, timing in self._model.generate_custom_voice_streaming(
            text=text,
            language=TTS_LANGUAGE,
            speaker=TTS_SPEAKER,
            chunk_size=8,
            max_new_tokens=TTS_MAX_NEW_TOKENS,
        ):
            if first:
                logger.info("TTS first chunk in %.2fs", time.monotonic() - t0)
                first = False
            total_steps = timing.get("total_steps_so_far", total_steps)
            yield audio_chunk, sr
        # The library's decode loop stops silently (no exception, no flag in
        # `timing`) whether it hit EOS naturally or ran out of budget -- the
        # only way to tell them apart from out here is that a budget-limited
        # stop lands (near) exactly on the step ceiling we asked for.
        if total_steps >= TTS_MAX_NEW_TOKENS - 8:
            logger.warning(
                "TTS hit its %d-token generation ceiling -- reply was likely cut off "
                "mid-sentence. Consider raising TTS_MAX_NEW_TOKENS/TTS_MAX_SEQ_LEN.",
                TTS_MAX_NEW_TOKENS,
            )
        logger.info("TTS generation done in %.1fs", time.monotonic() - t0)
