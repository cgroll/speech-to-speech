"""Orchestration: idle -> recording -> thinking -> speaking -> idle, driven
by a record toggle. One trigger toggles recording on/off, same as
parakeet-dictate; everything else (LLM call, TTS, playback) happens
automatically once a recording is stopped.

Two independent trigger sources call the same on_toggle(): the Jabra
button (evdev, read directly by this process) and a local Unix socket
(toggle_socket.py) that a GNOME-hotkey-bound CLI command talks to, for
toggling from the keyboard when you're at the desk. Single long-running
process holds both models warm and reacts to both directly -- no
daemon/socket split needed for the Jabra path, since (unlike a GNOME
hotkey) evdev lets this same process listen for the button itself.
"""

import logging
import threading

from speech_to_speech import audio_io, input_button, toggle_socket
from speech_to_speech.llm import ClaudeCodeConversation
from speech_to_speech.stt import SpeechToText
from speech_to_speech.tts import TextToSpeech

logger = logging.getLogger(__name__)


class App:
    def __init__(self) -> None:
        self._state = "idle"
        self._state_lock = threading.Lock()
        self._recorder = audio_io.Recorder()
        self._stt = SpeechToText()
        self._tts = TextToSpeech()
        self._llm: ClaudeCodeConversation | None = None
        self._worker: threading.Thread | None = None
        self._transcribed: list[str] = []

    def load(self) -> None:
        # Fail fast on a missing API key before spending time loading models.
        self._llm = ClaudeCodeConversation()
        self._stt.load()
        self._tts.load()
        logger.info("Ready. Press the Jabra button (or the local toggle hotkey) to start recording.")

    def on_toggle(self) -> None:
        # Two threads (evdev listener, socket server) can call this
        # concurrently -- claim the state transition inside a short lock so
        # a simultaneous Jabra + local-key press can't both see "idle" and
        # both start a recording. The lock is only held for this quick
        # check-and-claim, not across the (multi-second) handler calls
        # below, so a toggle from the other source during "thinking" or
        # "speaking" still gets an immediate "busy" response instead of
        # queuing up behind a lock.
        with self._state_lock:
            state = self._state
            if state == "idle":
                self._state = "recording"
            elif state == "recording":
                self._state = "thinking"
            else:
                logger.info("Busy (%s), ignoring toggle", state)
                return

        if state == "idle":
            self._start_recording()
        else:
            self._stop_recording_and_respond()

    def _start_recording(self) -> None:
        self._transcribed = []
        self._recorder.start()
        self._state = "recording"
        self._worker = threading.Thread(target=self._consume_segments, daemon=True)
        self._worker.start()
        logger.info("Recording... press the button again to stop.")

    def _consume_segments(self) -> None:
        for audio in self._recorder.segments():
            text = self._stt.transcribe(audio)
            if text:
                self._transcribed.append(text)

    def _stop_recording_and_respond(self) -> None:
        self._state = "thinking"
        self._recorder.stop()
        assert self._worker is not None
        self._worker.join()

        text = " ".join(self._transcribed).strip()
        if not text:
            logger.info("No speech detected.")
            self._state = "idle"
            return

        logger.info("Transcribed: %s", text)
        assert self._llm is not None
        reply = self._llm.send(text)

        self._state = "speaking"
        self._speak(reply)

        self._state = "idle"

    def _speak(self, reply: str) -> None:
        audio_io.play_audio_streaming(self._tts.synthesize_streaming(reply))

    def run(self) -> None:
        threading.Thread(
            target=toggle_socket.serve, args=(self.on_toggle,), daemon=True
        ).start()
        threading.Thread(target=self._run_jabra_listener, daemon=True).start()
        # Both triggers run in background threads; block here until Ctrl+C
        # so the app still works via the local hotkey alone if the Jabra
        # isn't plugged in.
        threading.Event().wait()

    def _run_jabra_listener(self) -> None:
        try:
            input_button.listen_for_toggle(self.on_toggle)
        except RuntimeError as exc:
            logger.warning("Jabra button unavailable (%s). Local toggle hotkey still works.", exc)
