"""Text-to-speech via Phonon (Gradium), CPU-only. Alternative backend to
tts.py's Qwen3-TTS, selected via config.TTS_BACKEND -- see daemon.py's
`_make_tts()`. Implements the same `load()`/`synthesize_streaming()`
interface as `tts.py`'s `TextToSpeech` so the rest of the daemon (playback,
watchdog, speak/synthesize/stop command handling) doesn't need to know which
backend is loaded.

Unlike Qwen3-TTS, Phonon (`ptts`) needs no GPU and has no KV-cache ceiling
that truncates long replies mid-sentence -- so this module skips tts.py's
`_split_for_tts()` chunking entirely and just streams the whole reply in one
`ptts` call.
"""

import logging
import time
from collections.abc import Iterator

import numpy as np

from speech_to_speech.config import PHONON_LANGUAGE, PHONON_MODEL_ID, PHONON_VOICE

logger = logging.getLogger(__name__)


class PhononTextToSpeech:
    def __init__(self) -> None:
        self._model = None

    def load(self) -> None:
        import ptts

        # `lang` is bound here, at load time -- ptts has no per-call language
        # argument, unlike Qwen3-TTS's per-request `language=`. See config.py's
        # PHONON_LANGUAGE comment for the resulting limitation (one accent for
        # the whole daemon lifetime, no auto-detection per utterance).
        t0 = time.monotonic()
        logger.info(
            "Loading TTS model (Phonon %s, lang=%s, CPU)...", PHONON_MODEL_ID, PHONON_LANGUAGE
        )
        self._model = ptts.TTS(model=PHONON_MODEL_ID, lang=PHONON_LANGUAGE)
        logger.info("TTS model loaded in %.1fs", time.monotonic() - t0)
        logger.info("TTS warmup done, ready.")

    def synthesize_streaming(self, text: str) -> Iterator[tuple[np.ndarray, int]]:
        text = text.strip()
        if not text:
            return
        t0 = time.monotonic()
        first = True
        sample_rate = self._model.sample_rate
        with self._model.stream(text, voice=PHONON_VOICE) as audio:
            for pcm in audio:
                if first:
                    logger.info("TTS first chunk in %.2fs", time.monotonic() - t0)
                    first = False
                yield pcm, sample_rate
        logger.info("TTS generation done in %.1fs", time.monotonic() - t0)
