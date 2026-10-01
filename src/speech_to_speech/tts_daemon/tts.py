"""Text-to-speech via Qwen3-TTS CustomVoice (GGML GPU). Fixed speaker
embedding, using faster-qwen3-tts with GGML quantization. Audio is yielded
in chunks as soon as they are ready so playback can start immediately."""

import logging
import re
import sys
import time
from collections.abc import Iterator

import numpy as np

from speech_to_speech.config import (
    TTS_CHUNK_CHAR_BUDGET,
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

# Sentence boundaries: after .!?… + whitespace, or any run of newlines. Used to
# chop a long reply into talker-sized pieces (TTS_CHUNK_CHAR_BUDGET) so no single
# generation overruns the KV-cache ceiling -- see _split_for_tts.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")


def _split_for_tts(text: str, budget: int) -> list[str]:
    """Split text into chunks of at most `budget` chars, breaking on sentence
    boundaries so each TTS generation stays well under the talker's KV-cache
    ceiling (config.TTS_CHUNK_CHAR_BUDGET explains why). Sentences are packed
    greedily; a lone sentence longer than the budget is hard-split on word
    boundaries as a last resort. Returns [] for empty/blank text."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= budget:
        return [text]

    chunks: list[str] = []
    current = ""
    for sentence in _SENTENCE_SPLIT.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > budget:
            if current:
                chunks.append(current)
                current = ""
            piece = ""
            for word in sentence.split():
                if piece and len(piece) + 1 + len(word) > budget:
                    chunks.append(piece)
                    piece = word
                else:
                    piece = f"{piece} {word}" if piece else word
            current = piece
        elif current and len(current) + 1 + len(sentence) > budget:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence
    if current:
        chunks.append(current)
    return chunks


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
        # Chunk long replies so no single generation overruns the talker's
        # KV-cache ceiling: on CUDA that ceiling truncates silently mid-sentence,
        # on the macOS ggml backend it crashes ("talker decode failed"). Each
        # chunk is a fresh, independent generation; the audio chunks stream out
        # back to back, so the caller (playback or _synthesize's collector) sees
        # one continuous stream regardless of how the text was split. Short
        # replies stay a single chunk -- the common case, unchanged.
        t0 = time.monotonic()
        first = True
        pieces = _split_for_tts(text, TTS_CHUNK_CHAR_BUDGET)
        if len(pieces) > 1:
            logger.info(
                "TTS splitting reply (%d chars) into %d chunks to stay under the talker ceiling",
                len(text),
                len(pieces),
            )
        for piece in pieces:
            for audio_chunk, sr, _timing in self._model.generate_custom_voice_streaming(
                text=piece,
                language=TTS_LANGUAGE,
                speaker=TTS_SPEAKER,
                chunk_size=8,
                max_new_tokens=TTS_MAX_NEW_TOKENS,
            ):
                if first:
                    logger.info("TTS first chunk in %.2fs", time.monotonic() - t0)
                    first = False
                yield audio_chunk, sr
        logger.info("TTS generation done in %.1fs", time.monotonic() - t0)
