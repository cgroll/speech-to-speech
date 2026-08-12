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
import time

from speech_to_speech import audio_io, cockpit, input_button, toggle_socket
from speech_to_speech.llm import ClaudeCodeConversation
from speech_to_speech.stt import SpeechToText
from speech_to_speech.tts import TextToSpeech

logger = logging.getLogger(__name__)

STATE_LABELS = {
    "idle": "Bereit",
    "recording": "Aufnahme läuft…",
    "thinking": "Denkt nach…",
    "speaking": "Spricht…",
}


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
        # Replaced with a fresh Event each time a recording is stopped
        # (recording -> thinking); shared by the thinking and speaking
        # phases of that same turn. A barge-in sets it to tell whichever
        # phase is currently in flight to abort instead of continuing.
        self._interrupt = threading.Event()
        # Chat history + timing stats for the web cockpit (docs/architecture
        # -proposal.md, "Web-Cockpit"). Guarded by their own lock, separate
        # from _state_lock, since cockpit reads/writes here don't need to
        # participate in the (short, latency-sensitive) state-transition
        # locking above.
        self._history_lock = threading.Lock()
        self._history: list[dict[str, str]] = []
        self._stats = {
            "turns": 0,
            "last_stt_s": None,
            "last_response_s": None,
            "last_ttfa_s": None,
            "last_speaking_s": None,
        }

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
                # Fresh Event, created atomically with the state flip so a
                # concurrent barge-in either sees the old "recording" state
                # (and is ignored) or sees "thinking" together with this
                # exact Event -- never a stale one from a previous turn.
                self._interrupt = threading.Event()
                self._state = "thinking"
            elif state in ("thinking", "speaking"):
                # Barge-in: give control back to the user instead of
                # ignoring the press. Flag whatever's in flight (LLM call or
                # playback) to abort, and jump straight into a new recording
                # -- no detour through idle. The interrupted handler thread
                # notices the flag on its own and unwinds without touching
                # state again.
                self._interrupt.set()
                self._state = "recording"
            else:
                logger.info("Busy (%s), ignoring toggle", state)
                return

        if state == "idle":
            self._start_recording()
        elif state == "recording":
            # Run off the calling thread (evdev read_loop or the socket
            # server's accept loop): that thread is the only thing that can
            # notice a barge-in, but _stop_recording_and_respond blocks for
            # the LLM call and the full TTS playback. Blocking it there would
            # make the trigger deaf to further presses for the whole turn --
            # on the socket path, the server wouldn't even accept() the next
            # connection until this one returned, silently dropping the
            # barge-in press instead of interrupting playback.
            threading.Thread(target=self._stop_recording_and_respond, daemon=True).start()
        else:
            logger.info("Barge-in: interrupting %s, starting new recording.", state)
            self._start_recording()
            if self._llm is not None:
                self._llm.cancel()

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
        # Capture the Event on_toggle created for this turn -- self._interrupt
        # will point at a *different* Event once the next turn starts, so a
        # local reference is what makes the checks below race-safe.
        interrupt = self._interrupt
        stop_t0 = time.monotonic()
        self._recorder.stop()
        assert self._worker is not None
        self._worker.join()
        # Mic-stop-to-text delay: stop() itself returns fast, so this is
        # dominated by _consume_segments() finishing off whatever speech
        # segments (including the final flushed one) hadn't been transcribed
        # yet -- the lag a user actually feels between letting go of the
        # button and something happening.
        stt_s = time.monotonic() - stop_t0
        # Known narrow gap: a barge-in landing exactly here (before the LLM
        # call below starts) races with the next _start_recording()'s reset
        # of self._transcribed. Accepted for now -- this feature targets the
        # multi-second "thinking"/"speaking" waits, not this sub-second one.

        text = " ".join(self._transcribed).strip()
        if not text:
            logger.info("No speech detected.")
            with self._state_lock:
                if self._state == "thinking":
                    self._state = "idle"
            return

        logger.info("Transcribed: %s", text)
        with self._history_lock:
            self._stats["last_stt_s"] = stt_s
        self._append_history("user", text)
        assert self._llm is not None
        t0 = time.monotonic()
        reply = self._llm.send(text)
        reply_ready = time.monotonic()
        response_s = reply_ready - t0

        if interrupt.is_set():
            logger.info("Interrupted while thinking -- discarding reply.")
            return

        with self._history_lock:
            self._stats["turns"] += 1
            self._stats["last_response_s"] = response_s
        self._append_history("assistant", reply)

        with self._state_lock:
            if self._state != "thinking":
                return  # a barge-in already claimed the state for a new recording
            self._state = "speaking"
        self._speak(reply, interrupt, reply_ready)

        with self._state_lock:
            if self._state == "speaking":
                self._state = "idle"

    def _speak(self, reply: str, interrupt: threading.Event, reply_ready: float) -> None:
        t0 = time.monotonic()

        def _on_first_chunk() -> None:
            # Time from "reply text is ready" to "first audio sample actually
            # written to the output stream" -- covers TTS-synthesis latency
            # for the first chunk *and* the ffmpeg atempo round-trip, so it's
            # the number that matches what a listener actually experiences
            # as the pause before the reply starts.
            with self._history_lock:
                self._stats["last_ttfa_s"] = time.monotonic() - reply_ready

        audio_io.play_audio_streaming(
            self._tts.synthesize_streaming(reply), stop_event=interrupt, on_first_chunk=_on_first_chunk
        )
        with self._history_lock:
            self._stats["last_speaking_s"] = time.monotonic() - t0

    def _append_history(self, role: str, content: str) -> None:
        with self._history_lock:
            self._history.append({"role": role, "content": content})

    # -- Cockpit-facing read/write API ------------------------------------
    # Called from cockpit.py's Gradio callbacks, which run on Gradio's own
    # request threads -- everything here either takes a lock already used
    # elsewhere (state_lock, history_lock) or, for on_toggle()/reset(), was
    # already designed to be called from more than one trigger thread.

    def get_state(self) -> str:
        return STATE_LABELS.get(self._state, self._state)

    def get_history(self) -> list[dict[str, str]]:
        with self._history_lock:
            return list(self._history)

    def get_stats_text(self) -> str:
        with self._history_lock:
            stats = dict(self._stats)

        def _fmt(key: str) -> str:
            value = stats[key]
            return f"{value:.1f}s" if value is not None else "–"

        return (
            f"Runden: {stats['turns']}  |  "
            f"STT-Zeit (ab Mikro-Stopp): {_fmt('last_stt_s')}  |  "
            f"Letzte Antwortzeit: {_fmt('last_response_s')}  |  "
            f"Zeit bis erste Sprachausgabe: {_fmt('last_ttfa_s')}  |  "
            f"Letzte Sprechzeit: {_fmt('last_speaking_s')}"
        )

    def reset(self) -> None:
        """Starts a fresh session (cockpit's "Neue Session" button): aborts
        whatever's in flight, reconnects a new Claude Agent SDK client so no
        conversation memory carries over, and clears history + stats.

        Deliberately reuses the same interrupt/cancel plumbing as barge-in
        (self._interrupt, ClaudeCodeConversation.cancel()) instead of adding
        a separate abort path -- a reset is just "barge-in, then also wipe
        the history" from the in-flight handler thread's point of view, so
        its existing "discard reply if interrupted" / "don't touch state if
        someone else already claimed it" checks apply unchanged.
        """
        with self._state_lock:
            state = self._state
            if state in ("thinking", "speaking"):
                self._interrupt.set()
            self._state = "idle"

        if state == "recording":
            self._recorder.stop()
            if self._worker is not None:
                self._worker.join()

        old_llm = self._llm
        if old_llm is not None and state in ("thinking", "speaking"):
            old_llm.cancel()
        self._llm = ClaudeCodeConversation()
        if old_llm is not None:
            old_llm.close()

        with self._history_lock:
            self._history = []
            self._stats = {
                "turns": 0,
                "last_stt_s": None,
                "last_response_s": None,
                "last_ttfa_s": None,
                "last_speaking_s": None,
            }
        logger.info("Session reset.")

    def run(self) -> None:
        threading.Thread(
            target=toggle_socket.serve, args=(self.on_toggle,), daemon=True
        ).start()
        threading.Thread(target=self._run_jabra_listener, daemon=True).start()
        threading.Thread(target=self._run_cockpit, daemon=True).start()
        # All triggers run in background threads; block here until Ctrl+C so
        # the app still works via the local hotkey alone if e.g. the Jabra
        # isn't plugged in or the cockpit's port is already taken.
        threading.Event().wait()

    def _run_jabra_listener(self) -> None:
        try:
            input_button.listen_for_toggle(self.on_toggle)
        except RuntimeError as exc:
            logger.warning("Jabra button unavailable (%s). Local toggle hotkey still works.", exc)

    def _run_cockpit(self) -> None:
        try:
            cockpit.run(self)
        except OSError as exc:
            logger.warning("Web cockpit unavailable (%s). Jabra button/local hotkey still work.", exc)
