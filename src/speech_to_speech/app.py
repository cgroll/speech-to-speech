"""Orchestration: idle -> recording -> thinking -> speaking -> idle, driven
by a record toggle. One trigger toggles recording on/off, same as
parakeet-dictate; everything else (LLM call, TTS, playback) happens
automatically once a recording is stopped.

Two independent trigger sources call the same on_toggle(): the Jabra
button (evdev, read directly by this process) and a local Unix socket
(toggle_socket.py) that a GNOME-hotkey-bound CLI command talks to, for
toggling from the keyboard when you're at the desk. This process holds no
models itself -- STT and TTS both live in shared daemons (stt_client.py,
tts_client.py) -- and reacts to both trigger sources directly; no
daemon/socket split was needed for the Jabra path itself, since (unlike a
GNOME hotkey) evdev lets this same process listen for the button.
"""

import logging
import threading
import time

import numpy as np

from speech_to_speech import cockpit, input_button, sessions, stt_client, toggle_socket, tts_client
from speech_to_speech.agent_backend import AGENT_LABELS, AgentConversation, DEFAULT_AGENT, create_conversation

logger = logging.getLogger(__name__)

STATE_LABELS = {
    "idle": "Bereit",
    "recording": "Aufnahme läuft…",
    "thinking": "Denkt nach…",
    "speaking": "Spricht…",
}


class App:
    def __init__(self, default_agent: str = DEFAULT_AGENT) -> None:
        self._state = "idle"
        self._state_lock = threading.Lock()
        # Mic capture/STT and TTS synthesis/playback both live in shared
        # daemons now (speech_to_speech.dictate.daemon,
        # speech_to_speech.tts_daemon.daemon -- docs/architecture-proposal.md
        # "Daemon-Aufspaltung"). stt_client/tts_client just talk to them over
        # Unix sockets, no model state to hold here anymore.
        self._llm: AgentConversation | None = None
        # Which backend `_llm` currently is (agent_backend.AGENT_CLAUDE /
        # AGENT_PI) -- set alongside every `self._llm =` assignment below.
        # Drives the cockpit's current-agent display and the tag a new
        # session is recorded under (docs/backlog.md, "Mehrere
        # Agent-Backends", decision 1: sessions carry an agent label).
        self._default_agent = default_agent
        self._agent_name = default_agent
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
        # Fail fast on a missing API key or an unreachable STT/TTS daemon
        # before the app starts listening for a toggle.
        self._llm = create_conversation(self._default_agent)
        self._agent_name = self._default_agent
        stt_client.ensure_available()
        tts_client.ensure_available()
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
            if state == "speaking":
                tts_client.stop()
            if self._llm is not None:
                self._llm.cancel()

    def _start_recording(self) -> None:
        stt_client.start_recording()
        self._state = "recording"
        logger.info("Recording... press the button again to stop.")

    def _stop_recording_and_respond(self) -> None:
        # Capture the Event on_toggle created for this turn -- self._interrupt
        # will point at a *different* Event once the next turn starts, so a
        # local reference is what makes the checks below race-safe.
        interrupt = self._interrupt
        stop_t0 = time.monotonic()
        # Mic-stop-to-text delay: the daemon's stop_recording call blocks
        # until it's drained whatever speech segments (including the final
        # flushed one) hadn't been transcribed yet -- the lag a user
        # actually feels between letting go of the button and something
        # happening.
        text = stt_client.stop_recording().strip()
        stt_s = time.monotonic() - stop_t0

        if not text:
            logger.info("No speech detected.")
            with self._state_lock:
                if self._state == "thinking":
                    self._state = "idle"
            return

        logger.info("Transcribed: %s", text)
        with self._history_lock:
            self._stats["last_stt_s"] = stt_s
        self._respond(text, interrupt)

    def submit_text(self, text: str) -> None:
        """Text-input path for the cockpit: lets you type or paste text
        instead of speaking (handy for quickly dropping in a chunk of text
        that would be awkward to dictate). Skips recording/STT entirely and
        jumps straight to "thinking", but otherwise follows the exact same
        thinking -> speaking -> idle flow -- including barge-in semantics --
        as a voice turn, by sharing _respond() with
        _stop_recording_and_respond().
        """
        text = text.strip()
        if not text:
            return

        with self._state_lock:
            state = self._state
            if state == "idle":
                self._interrupt = threading.Event()
                self._state = "thinking"
            elif state in ("thinking", "speaking"):
                # Barge-in, same idea as on_toggle()'s: flag whatever's in
                # flight to abort. Unlike the mic path there's no "recording"
                # phase for this turn to create its own fresh Event at the
                # end of, so it's created here instead, right away.
                self._interrupt.set()
                self._interrupt = threading.Event()
                self._state = "thinking"
            else:
                logger.info("Busy (%s), ignoring text submit", state)
                return
            interrupt = self._interrupt

        logger.info("Text input: %s", text)
        if state in ("thinking", "speaking"):
            assert self._llm is not None
            if state == "speaking":
                tts_client.stop()
            self._llm.cancel()
        threading.Thread(target=self._respond, args=(text, interrupt), daemon=True).start()

    def _respond(self, text: str, interrupt: threading.Event) -> None:
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
                return  # a barge-in already claimed the state for a new turn
            self._state = "speaking"
        self._speak(reply)

        with self._state_lock:
            if self._state == "speaking":
                self._state = "idle"

    def _speak(self, reply: str) -> None:
        # Synthesis and playback both happen inside the TTS daemon now
        # (speech_to_speech.tts_daemon.daemon) -- this call blocks until
        # playback finishes or a barge-in's tts_client.stop() cancels it.
        # "Time to first audio" is measured daemon-side and returned once
        # the call completes, since the cockpit stat is only read after the
        # fact anyway.
        t0 = time.monotonic()
        first_chunk_s = tts_client.speak(reply)
        with self._history_lock:
            if first_chunk_s is not None:
                self._stats["last_ttfa_s"] = first_chunk_s
            self._stats["last_speaking_s"] = time.monotonic() - t0

    def voice_turn(self, audio: np.ndarray) -> tuple[np.ndarray, int] | None:
        """Handles one complete, already-recorded turn from a remote client
        (the Gradio cockpit's mic widget, reached e.g. over Tailscale from a
        phone -- see docs/architecture-proposal.md, "Offene Frage: mobiler
        Zugriff (Handy)"): transcribe -> agent -> synthesize, end to end.
        `audio` must already be 16 kHz mono float32 (see cockpit.py's
        conversion from whatever the browser recorded); returns the
        synthesized reply as (samples, sample_rate) instead of playing it on
        this machine's speakers (tts_client.synthesize(), not speak()), or
        None if there was nothing to say (busy, no speech detected, or an
        empty/cancelled synthesis).

        A first, simple round trip: blocks the calling thread for the whole
        turn, no barge-in/streaming yet -- same "busy, ignoring" behavior as
        submit_text() rather than on_toggle()'s richer interrupt handling,
        since there's no in-progress recording/thinking/speaking phase of
        *this* turn for a second call to interrupt partway through."""
        with self._state_lock:
            if self._state != "idle":
                logger.info("Busy (%s), ignoring voice turn", self._state)
                return None
            self._interrupt = threading.Event()
            self._state = "thinking"

        text = stt_client.transcribe(audio).strip()
        if not text:
            logger.info("No speech detected (voice turn).")
            with self._state_lock:
                self._state = "idle"
            return None

        logger.info("Voice turn (remote): %s", text)
        self._append_history("user", text)
        assert self._llm is not None
        reply = self._llm.send(text)
        self._append_history("assistant", reply)
        with self._history_lock:
            self._stats["turns"] += 1

        with self._state_lock:
            self._state = "speaking"
        reply_audio, sample_rate = tts_client.synthesize(reply)
        with self._state_lock:
            self._state = "idle"

        if reply_audio.size == 0:
            return None
        return reply_audio, sample_rate

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

        agent_label = AGENT_LABELS.get(self._agent_name, self._agent_name)
        return (
            f"Agent: {agent_label}  |  "
            f"Runden: {stats['turns']}  |  "
            f"STT-Zeit (ab Mikro-Stopp): {_fmt('last_stt_s')}  |  "
            f"Letzte Antwortzeit: {_fmt('last_response_s')}  |  "
            f"Zeit bis erste Sprachausgabe: {_fmt('last_ttfa_s')}  |  "
            f"Letzte Sprechzeit: {_fmt('last_speaking_s')}"
        )

    def get_agent_name(self) -> str:
        return self._agent_name

    def _abort_current_turn(self) -> AgentConversation | None:
        """Shared first half of reset() and resume_session(): claims idle,
        aborts whatever's in flight (same interrupt/cancel plumbing as
        barge-in -- self._interrupt, ClaudeCodeConversation.cancel() --
        rather than a separate abort path, so the in-flight handler
        thread's existing "discard reply if interrupted" / "don't touch
        state if someone else already claimed it" checks apply unchanged),
        and hands back the old LLM client (still open) for the caller to
        replace and close once its replacement is ready."""
        with self._state_lock:
            state = self._state
            if state in ("thinking", "speaking"):
                self._interrupt.set()
            self._state = "idle"

        if state == "recording":
            stt_client.stop_recording()  # discard text, just stop+drain the daemon
        if state == "speaking":
            tts_client.stop()

        old_llm = self._llm
        if old_llm is not None and state in ("thinking", "speaking"):
            old_llm.cancel()
        return old_llm

    def reset(self, agent: str | None = None) -> None:
        """Starts a fresh session (cockpit's "Neue Session" button): aborts
        whatever's in flight, connects a fresh backend client so no
        conversation memory carries over, and clears history + stats.

        `agent` picks the backend for the new session (docs/backlog.md,
        "Mehrere Agent-Backends", decision 3: chosen per new session, not
        just once at app start) -- defaults to whichever backend was active,
        so calling reset() with no argument (e.g. any future non-cockpit
        caller) keeps today's behaviour of just restarting the same one."""
        agent = agent or self._agent_name
        old_llm = self._abort_current_turn()
        self._llm = create_conversation(agent)
        self._agent_name = agent
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

    def resume_session(self, choice: str | None) -> None:
        """Resumes a past session picked in the cockpit's session list
        (docs/backlog.md, "Frühere Sessions wieder aufnehmen können"/"Mehrere
        Agent-Backends"): same abort/teardown as reset(), but the fresh
        backend client is told to `resume` the given session_id instead of
        starting empty, and the cockpit history is rebuilt from that
        session's own transcript (sessions.load_session_history()) so the
        chat pane reflects the restored context too -- not just the LLM's
        internal memory of it.

        `choice` is a session-list value from sessions.session_choices(),
        packed as "<agent>:<session_id>" -- resuming always continues with
        whichever backend originally created that session (no backend
        switch mid-session), which is exactly what picking it back out of
        the tagged list means."""
        if not choice or ":" not in choice:
            return
        agent, session_id = choice.split(":", 1)

        old_llm = self._abort_current_turn()
        self._llm = create_conversation(agent, resume=session_id)
        self._agent_name = agent
        if old_llm is not None:
            old_llm.close()

        restored = sessions.load_session_history(agent, session_id)
        with self._history_lock:
            self._history = restored
            self._stats = {
                "turns": sum(1 for turn in restored if turn["role"] == "assistant"),
                "last_stt_s": None,
                "last_response_s": None,
                "last_ttfa_s": None,
                "last_speaking_s": None,
            }
        logger.info("Resumed session %s (%d restored turns).", session_id, len(restored))

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
