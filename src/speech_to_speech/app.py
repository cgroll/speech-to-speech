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
    CATEGORY_OTHER,
    CATEGORY_THINKING,
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

# gr.ChatMessage's `metadata.title` for each non-response output category
# (cockpit.py renders any history entry carrying `metadata` as Gradio's
# built-in collapsible "thought" bubble -- visually distinct from a normal
# reply automatically, no custom CSS needed). Never shown via TTS: _speak()
# is only ever called with send()'s own return value (see _respond()),
# these never reach it.
OUTPUT_CATEGORY_TITLES = {
    CATEGORY_THINKING: "🤔 Denkprozess",
    CATEGORY_OTHER: "🔧 Sonstiges (Tool-Aufruf/-Ergebnis)",
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
        # Cockpit "Audio stumm" checkbox (text-only mode): when set, _speak()
        # and voice_turn() skip actual TTS synthesis/playback but the normal
        # thinking -> speaking -> idle state flow and chat history still run
        # unchanged -- only the audio is suppressed, so typed/voice replies
        # still show up in the chat pane right away.
        self._voice_muted = False
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
        self._input_queue: list[str] = []
        self._responder_loop_running = False
        # Flag set during recording or when a new text input is submitted
        # while a turn is still active: prevents any *new* audio from
        # starting until the user is done with their input and the resulting
        # turn starts.
        self._audio_suppressed = False
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
            on_output=self._append_output,
            voice_output=True,
        )
        self._agent_name = self._default_agent
        stt_client.ensure_available()
        tts_client.ensure_available()
        logger.info("Ready. Press the Jabra button (or the local toggle hotkey) to start recording.")

    def _set_state(self, new_state: str) -> None:
        logger.info("State transition: %s -> %s", self._state, new_state)
        self._state = new_state

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
                self._set_state("recording")
                self._audio_suppressed = True
            elif state == "recording":
                self._set_state("thinking")
            elif state in ("thinking", "speaking"):
                # Barge-in: start recording and suppress current/future audio.
                # Unlike the old model, we DON'T set self._interrupt or call
                # llm.cancel() here -- we want the current turn to finish
                # its text-side work (Steering Message behavior). We only
                # stop the audio.
                self._set_state("recording")
                self._audio_suppressed = True
            else:
                logger.info("Busy (%s), ignoring toggle", state)
                return

        if state == "idle":
            self._start_recording()
        elif state == "recording":
            # Start the responder loop if not already running.
            # (In the new model, we might already have one running from a
            # previous turn that is now processing a queued message).
            threading.Thread(target=self._run_responder_loop, daemon=True).start()
        else:
            logger.info("Barge-in: suppressing audio, starting new recording.")
            tts_client.stop()
            self._start_recording()

    def _start_recording(self) -> None:
        stt_client.start_recording()
        logger.info("Recording... press the button again to stop.")

    def _run_responder_loop(self) -> None:
        """Central loop that drains the input queue. Started whenever a
        recording is stopped or text is submitted while idle."""
        with self._state_lock:
            if self._responder_loop_running:
                return
            self._responder_loop_running = True
        
        logger.info("Responder loop started.")
        try:
            # Drain the mic first if we just came from recording
            if stt_client.is_recording():
                stop_t0 = time.monotonic()
                text = stt_client.stop_recording().strip()
                stt_s = time.monotonic() - stop_t0
                if text:
                    logger.info("Transcribed: %s", text)
                    with self._history_lock:
                        self._stats["last_stt_s"] = stt_s
                    with self._state_lock:
                        self._input_queue.append(text)
                else:
                    logger.info("No speech detected.")

            while True:
                with self._state_lock:
                    if not self._input_queue:
                        logger.info("Input queue empty, exiting loop.")
                        # Nothing left to do for now. If we were thinking/speaking,
                        # go back to idle.
                        if self._state in ("thinking", "speaking"):
                            self._set_state("idle")
                        return
                    text = self._input_queue.pop(0)
                    # If a recording started while we were between queue items,
                    # don't start the next turn yet -- the on_toggle(stop) will
                    # spawn a new responder loop.
                    if self._state == "recording":
                        logger.info("Recording in progress, putting back input and exiting loop.")
                        self._input_queue.insert(0, text) # put it back
                        return
                    self._set_state("thinking")
                    self._audio_suppressed = False

                logger.info("Processing input from queue: %s", text)
                self._respond(text)
        finally:
            with self._state_lock:
                self._responder_loop_running = False

    def submit_text(self, text: str) -> None:
        """Text-input path for the cockpit: lets you type or paste text
        instead of speaking. Queues the input and ensures the responder
        loop is running."""
        text = text.strip()
        if not text:
            return

        logger.info("Text input: %s", text)
        start_loop = False
        with self._state_lock:
            self._input_queue.append(text)
            # Suppress audio for anything currently playing/pending
            self._audio_suppressed = True
            tts_client.stop()
            
            if self._state == "idle":
                self._set_state("thinking")
                self._audio_suppressed = False # Only suppressed until we start processing
                start_loop = True
            elif self._state == "recording":
                # Already have a responder loop waiting for recording to stop
                pass
            else:
                # Already thinking/speaking; the loop will pick up the new item
                pass
        
        if start_loop:
            threading.Thread(target=self._run_responder_loop, daemon=True).start()

    def _respond(self, text: str) -> None:
        self._append_history("user", text)
        assert self._llm is not None
        t0 = time.monotonic()
        try:
            reply = self._llm.send(text)
        except LlmTimeoutError:
            logger.warning("LLM turn timed out -- resetting to idle.")
            self._recover_from_llm_timeout()
            return
        
        reply_ready = time.monotonic()
        response_s = reply_ready - t0

        with self._history_lock:
            self._stats["turns"] += 1
            self._stats["last_response_s"] = response_s
        self._append_history("assistant", reply)

        # Check if we should speak this reply
        with self._state_lock:
            if self._audio_suppressed or self._state == "recording":
                logger.info("Audio suppressed or recording -- skipping playback of reply.")
                return
            self._set_state("speaking")
        
        self._speak(reply)

    def _speak(self, reply: str) -> None:
        if self._voice_muted:
            logger.info("Voice output muted -- skipping playback.")
            return
        # Synthesis and playback both happen inside the TTS daemon now
        # (speech_to_speech.tts_daemon.daemon) -- this call blocks until
        # playback finishes or a barge-in's tts_client.stop() cancels it.
        # "Time to first audio" is measured daemon-side and returned once
        # the call completes, since the cockpit stat is only read after the
        # fact anyway.
        t0 = time.monotonic()
        try:
            first_chunk_s = tts_client.speak(reply)
        except RuntimeError as exc:
            # The daemon can reject or vanish out from under us: a barge-in's
            # tts_client.stop() racing this call's own request ("busy:
            # speaking", see tts_daemon/daemon.py's _claim_speaking), or the
            # process having crashed with no supervisor to restart it on
            # macOS (DaemonUnavailableError, a RuntimeError subclass -- see
            # daemon_launch.py). Previously uncaught here, which killed this
            # thread before the state-reset in _respond()/
            # _recover_from_llm_timeout ran, leaving App stuck in "speaking"
            # until a manual barge-in forced it back to "recording". Log and
            # return so the caller's normal reset-to-idle still happens.
            logger.warning("TTS playback failed (%s) -- treating turn as done.", exc)
            return
        with self._history_lock:
            if first_chunk_s is not None:
                self._stats["last_ttfa_s"] = first_chunk_s
            self._stats["last_speaking_s"] = time.monotonic() - t0

    def _recover_from_llm_timeout(self) -> None:
        """Shared LlmTimeoutError handling: tell the user and reset."""
        with self._state_lock:
            if self._audio_suppressed or self._state == "recording":
                return
            self._set_state("speaking")
        
        self._append_history("assistant", TIMEOUT_MESSAGE)
        self._speak(TIMEOUT_MESSAGE)

    def voice_turn(self, audio: np.ndarray) -> tuple[np.ndarray, int] | None:
        """Handles one complete, already-recorded turn from a remote client."""
        # Simple blocking implementation, doesn't use the queue for now
        # as it's a one-shot remote call.
        with self._state_lock:
            if self._state != "idle":
                logger.info("Busy (%s), ignoring voice turn", self._state)
                return None
            self._set_state("thinking")

        text = stt_client.transcribe(audio).strip()
        if not text:
            logger.info("No speech detected (voice turn).")
            with self._state_lock:
                self._set_state("idle")
            return None

        logger.info("Voice turn (remote): %s", text)
        self._append_history("user", text)
        assert self._llm is not None
        try:
            reply = self._llm.send(text)
        except LlmTimeoutError:
            logger.warning("LLM turn timed out (voice_turn) -- resetting to idle.")
            self._append_history("assistant", TIMEOUT_MESSAGE)
            reply_audio, sample_rate = tts_client.synthesize(TIMEOUT_MESSAGE)
            with self._state_lock:
                self._set_state("idle")
            return reply_audio, sample_rate
            
        self._append_history("assistant", reply)
        with self._history_lock:
            self._stats["turns"] += 1

        if self._voice_muted:
            with self._state_lock:
                self._set_state("idle")
            return None
            
        reply_audio, sample_rate = tts_client.synthesize(reply)
        with self._state_lock:
            self._set_state("idle")
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

    def _append_output(self, category: str, text: str) -> None:
        """create_conversation()'s `on_output` callback: appends a
        CATEGORY_THINKING/CATEGORY_OTHER chunk to the chat history as its
        own entry, live, as the backend produces it -- *not* via
        _speak()/TTS (that only ever gets called with send()'s own return
        value, the actual response, see _respond()/voice_turn() -- this
        method is never in that path). Tagged with `metadata` so
        cockpit.py's gr.Chatbot renders it as a visually distinct
        collapsible "thought" bubble instead of a normal reply. Same
        threading note as _append_image(): may run on a backend's own
        background thread (Claude SDK's event loop; Pi's send() call runs
        on whatever thread called it, so no extra thread there), never
        assume the caller already holds _history_lock."""
        title = OUTPUT_CATEGORY_TITLES.get(category, f"🔧 {category}")
        with self._history_lock:
            self._history.append(
                {
                    "role": "assistant",
                    "content": text,
                    "metadata": {"title": title, "status": "done"},
                }
            )

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

    def get_voice_muted(self) -> bool:
        return self._voice_muted

    def set_voice_muted(self, muted: bool) -> None:
        self._voice_muted = muted
        logger.info("Voice output %s.", "muted" if muted else "unmuted")

    def stop(self) -> None:
        """Dedicated cockpit "Stop" button: cancels whatever's in flight
        (a pending LLM call or TTS playback) and returns straight to idle --
        unlike on_toggle()'s barge-in handling, which treats a press during
        "thinking"/"speaking" as the start of a *new* recording. This is for
        the plain "stop talking, I don't want to record anything right now"
        case (e.g. a long answer droning on) that barge-in doesn't cover.
        Reuses _abort_current_turn() (also used by reset()/resume_session())
        but, unlike those, keeps the current conversation and history
        intact -- just interrupts and discards whatever reply was in
        flight."""
        self._abort_current_turn()

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
                self._set_state("idle")
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
                self._set_state("idle")
        if state == "speaking":
            tts_client.stop()
        daemon_control.restart_tts_daemon()
        logger.info("TTS daemon restarted.")

    def _abort_current_turn(self) -> AgentConversation | None:
        """Claims idle, clears queues, and stops audio."""
        with self._state_lock:
            self._input_queue = []
            self._audio_suppressed = True
            self._set_state("idle")

        if stt_client.is_recording():
            stt_client.stop_recording()
        tts_client.stop()

        old_llm = self._llm
        if old_llm is not None:
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
            agent,
            workspace=workspace,
            on_image=self._append_image,
            on_output=self._append_output,
            voice_output=True,
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
            on_output=self._append_output,
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
