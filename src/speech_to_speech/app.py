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

from speech_to_speech import cockpit, daemon_control, input_button, sessions, stt_client, toggle_socket, tts_client
from speech_to_speech.agent_backend import (
    AGENT_LABELS,
    AgentConversation,
    DEFAULT_AGENT,
    LlmTimeoutError,
    create_conversation,
)
from speech_to_speech.config import DEFAULT_WORKSPACE

# Spoken/shown when a backend gives up on a stuck turn (agent_backend.
# LlmTimeoutError) -- tells the user plainly rather than leaving them
# staring at a stuck "Denkt nach…" with no idea the turn already died.
TIMEOUT_MESSAGE = "Entschuldigung, das hat zu lange gedauert. Versuch's gern nochmal."

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
        # Working directory the agent backend's file/tool access is rooted
        # in (docs/backlog.md, "Mehrere Agent-Backends", point 2) -- same
        # "picked once at session start, no mid-session switch" model as
        # _agent_name above. Defaults to DEFAULT_WORKSPACE; changed via
        # reset()'s workspace argument (cockpit's folder picker feeds it).
        self._workspace = str(DEFAULT_WORKSPACE)
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
        # content is usually a str (a text turn), but _append_image() below
        # also appends a mixed list[dict|str] (file dict + caption) for an
        # image the agent showed via the show_image tool -- both shapes are
        # what gr.Chatbot's message format accepts directly (cockpit.py).
        self._history: list[dict[str, object]] = []
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
        self._llm = create_conversation(
            self._default_agent,
            workspace=self._workspace,
            on_image=self._append_image,
            voice_output=True,
        )
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
        try:
            reply = self._llm.send(text)
        except LlmTimeoutError:
            logger.warning("LLM turn timed out -- resetting to idle.")
            self._recover_from_llm_timeout(interrupt)
            return
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

    def _recover_from_llm_timeout(self, interrupt: threading.Event) -> None:
        """Shared LlmTimeoutError handling for _respond()/voice_turn(): the
        backend has already given up on (and best-effort interrupted) a
        stuck turn (agent_backend.LlmTimeoutError), so unlike a normal reply
        there's nothing to discard-if-interrupted -- just tell the user in
        their own ears (same speaking->idle path a real reply takes, so
        state comes back the same way either way) and free up "thinking" so
        the next turn isn't left waiting behind a dead one forever."""
        if interrupt.is_set():
            logger.info("Interrupted while thinking -- timeout recovery moot.")
            return

        self._append_history("assistant", TIMEOUT_MESSAGE)

        with self._state_lock:
            if self._state != "thinking":
                return  # a barge-in already claimed the state for a new turn
            self._state = "speaking"
        self._speak(TIMEOUT_MESSAGE)

        with self._state_lock:
            if self._state == "speaking":
                self._state = "idle"

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
        try:
            reply = self._llm.send(text)
        except LlmTimeoutError:
            logger.warning("LLM turn timed out (voice_turn) -- resetting to idle.")
            self._append_history("assistant", TIMEOUT_MESSAGE)
            with self._state_lock:
                self._state = "speaking"
            reply_audio, sample_rate = tts_client.synthesize(TIMEOUT_MESSAGE)
            with self._state_lock:
                self._state = "idle"
            if reply_audio.size == 0:
                return None
            return reply_audio, sample_rate
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

    def _append_image(self, path: str, caption: str) -> None:
        """The show_image tool's delivery callback (image_tool.py, wired in
        via create_conversation()'s on_image) for the cockpit: appends a
        message whose content is a list mixing a Gradio file dict and the
        caption text, which gr.Chatbot renders as an inline image (verified
        against Gradio's own FileMessage/MessageDict shapes) -- both in one
        chat bubble, in one call to this method rather than two separate
        _append_history() calls. Runs on the Claude SDK's own background
        event-loop thread, not any thread already holding _history_lock, so
        this needs its own locking same as _append_history() above."""
        content: list[dict[str, str] | str] = [{"path": path}]
        if caption:
            content.append(caption)
        with self._history_lock:
            self._history.append({"role": "assistant", "content": content})

    # -- Cockpit-facing read/write API ------------------------------------
    # Called from cockpit.py's Gradio callbacks, which run on Gradio's own
    # request threads -- everything here either takes a lock already used
    # elsewhere (state_lock, history_lock) or, for on_toggle()/reset(), was
    # already designed to be called from more than one trigger thread.

    def get_state(self) -> str:
        return STATE_LABELS.get(self._state, self._state)

    def get_history(self) -> list[dict[str, object]]:
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
            f"Workspace: {self._workspace}  |  "
            f"Runden: {stats['turns']}  |  "
            f"STT-Zeit (ab Mikro-Stopp): {_fmt('last_stt_s')}  |  "
            f"Letzte Antwortzeit: {_fmt('last_response_s')}  |  "
            f"Zeit bis erste Sprachausgabe: {_fmt('last_ttfa_s')}  |  "
            f"Letzte Sprechzeit: {_fmt('last_speaking_s')}"
        )

    def get_agent_name(self) -> str:
        return self._agent_name

    def get_workspace(self) -> str:
        return self._workspace

    def restart_stt_daemon(self) -> None:
        """Manual escape hatch (docs/backlog.md, "Daemon-Neustart aus der
        App/dem Cockpit heraus") for when the STT daemon is still answering
        but stuck/misbehaving, without waiting for a crash the watchdog
        would catch. If a recording is in progress, it's stopped and
        discarded first -- same as a barge-in dropping into idle -- so the
        daemon isn't restarted out from under an in-flight mic-capture call;
        "thinking"/"speaking" don't touch the STT daemon at all and are left
        running untouched."""
        with self._state_lock:
            state = self._state
            if state == "recording":
                self._state = "idle"
        if state == "recording":
            stt_client.stop_recording()  # discard text, just stop+drain
        daemon_control.restart_stt_daemon()
        logger.info("STT daemon restarted.")

    def restart_tts_daemon(self) -> None:
        """Same idea as restart_stt_daemon(), for the TTS daemon: stops
        playback in flight first (same tts_client.stop() barge-in uses) so
        the daemon isn't restarted mid-speak()."""
        with self._state_lock:
            state = self._state
            if state == "speaking":
                self._state = "idle"
        if state == "speaking":
            tts_client.stop()
        daemon_control.restart_tts_daemon()
        logger.info("TTS daemon restarted.")

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

    def reset(self, agent: str | None = None, workspace: str | None = None) -> None:
        """Starts a fresh session (cockpit's "Neue Session" button): aborts
        whatever's in flight, connects a fresh backend client so no
        conversation memory carries over, and clears history + stats.

        `agent` picks the backend for the new session (docs/backlog.md,
        "Mehrere Agent-Backends", decision 3: chosen per new session, not
        just once at app start) -- defaults to whichever backend was active,
        so calling reset() with no argument (e.g. any future non-cockpit
        caller) keeps today's behaviour of just restarting the same one.

        `workspace` (docs/backlog.md, "Mehrere Agent-Backends", point 2)
        works the same way -- defaults to the currently active workspace.
        Callers that let a user type/pick an arbitrary path (cockpit,
        Telegram bot) are expected to have already validated it via
        agent_backend.resolve_workspace() before calling in, so this trusts
        the value as-is."""
        agent = agent or self._agent_name
        workspace = workspace or self._workspace
        old_llm = self._abort_current_turn()
        self._llm = create_conversation(
            agent, workspace=workspace, on_image=self._append_image, voice_output=True
        )
        self._agent_name = agent
        self._workspace = workspace
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
        the tagged list means.

        Workspace isn't tagged per past session anywhere (unlike the
        agent), so this keeps whatever `self._workspace` currently is
        rather than trying to recover what the original session used --
        fine as long as the resumed conversation's own file references are
        still valid from the new workspace, but a mismatch is possible if
        the workspace was since changed."""
        if not choice or ":" not in choice:
            return
        agent, session_id = choice.split(":", 1)

        old_llm = self._abort_current_turn()
        self._llm = create_conversation(
            agent,
            resume=session_id,
            workspace=self._workspace,
            on_image=self._append_image,
            voice_output=True,
        )
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
