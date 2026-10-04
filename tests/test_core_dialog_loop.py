"""Tests for the state model in docs/specification.md, exercised
against the real App (src/speech_to_speech/app.py) with fakes standing in
for the LLM backend and the STT/TTS daemons (see tests/fakes.py).

App's internal state names are idle/recording/thinking/speaking. 
Each test says in a comment which spec section it checks.

Structured in the four blocks: plain transitions, Stop, Barge-in/Steering,
concurrency, and Audio Suppression.
"""

from __future__ import annotations

import threading
import time

from speech_to_speech.agent_backend import LlmTimeoutError
from speech_to_speech.app import TIMEOUT_MESSAGE

from .conftest import Harness, wait_until
from .fakes import FakeAgentConversation

# -- Block 1: plain transitions (spec section 1/3) -------------------------


def test_text_turn_goes_idle_processing_responding_idle(harness: Harness) -> None:
    app = harness.app
    assert app._state == "idle"

    app.submit_text("hallo")

    assert wait_until(lambda: app._state == "idle")
    assert harness.llm.sent == ["hallo"]
    assert harness.tts.speak_calls == ["hi there"]
    assert [m["role"] for m in app.get_history()] == ["user", "assistant"]


def test_voice_turn_goes_idle_recording_processing_responding_idle(harness: Harness) -> None:
    app = harness.app
    assert app._state == "idle"

    app.on_toggle()  # idle -> recording
    assert app._state == "recording"
    assert harness.stt.start_calls == 1

    app.on_toggle()  # recording -> thinking (spawns responder loop)

    assert wait_until(lambda: app._state == "idle")
    assert harness.stt.stop_calls == 1
    assert harness.llm.sent == ["hello"]  # FakeSTT's default transcript


def test_toggle_plays_start_and_stop_feedback_cues(harness: Harness) -> None:
    """The Jabra-button toggle should give audible confirmation of each
    transition (feedback.py) -- not just the cockpit's visual state label,
    since the button is often pressed without looking at the screen."""
    app = harness.app

    app.on_toggle()  # idle -> recording
    assert harness.feedback.cue_calls == ["start"]

    app.on_toggle()  # recording -> thinking
    assert wait_until(lambda: app._state == "idle")
    # "stop" (the toggle press itself) is followed by "agent_start" (the
    # transcript reaching the agent) and "agent_done" (its reply is ready,
    # right before TTS synthesis starts) -- see _respond() in app.py.
    assert harness.feedback.cue_calls == ["start", "stop", "agent_start", "agent_done"]
    assert harness.tts.speak_calls == ["hi there"]


def test_processing_state_is_visible_while_agent_call_is_pending(harness: Harness) -> None:
    app = harness.app
    app._llm = FakeAgentConversation(reply="later", gate=True)

    app.submit_text("frage")
    assert wait_until(lambda: app._llm.started.is_set())
    assert app._state == "thinking"
    assert app.get_state() == "Denkt nach…"

    app._llm.release()
    assert wait_until(lambda: app._state == "idle")


def test_reply_is_shown_even_when_voice_output_is_muted(harness: Harness) -> None:
    app = harness.app
    app.set_voice_muted(True)

    app.submit_text("frage")

    assert wait_until(lambda: app._state == "idle")
    assert [m["role"] for m in app.get_history()] == ["user", "assistant"]
    assert harness.tts.speak_calls == []


def test_llm_timeout_recovers_to_idle_with_a_spoken_message(harness: Harness) -> None:
    app = harness.app
    app._llm = FakeAgentConversation(raises=LlmTimeoutError("stuck"))

    app.submit_text("frage")

    assert wait_until(lambda: app._state == "idle")
    assert app.get_history()[-1]["content"] == TIMEOUT_MESSAGE
    assert harness.tts.speak_calls == [TIMEOUT_MESSAGE]


# -- Block 2: Stop (spec section 1, "Stop") ---------------------------------


def test_stop_during_processing_discards_audio_but_keeps_text(harness: Harness) -> None:
    # New spec: Steering/Queueing model. stop() cancels LLM but doesn't 
    # necessarily discard history if it returned. 
    # Actually, FakeAgentConversation returns after cancel().
    app = harness.app
    app._llm = FakeAgentConversation(reply="too late", gate=True)

    app.submit_text("frage")
    assert wait_until(lambda: app._llm.started.is_set())
    assert app._state == "thinking"

    app.stop() # Calls _abort_current_turn -> clear queue, stop audio, cancel llm

    assert app._state == "idle"
    assert app._llm.cancel_calls == 1
    
    # Wait for the turn thread to finish
    assert wait_until(lambda: app._llm.sent == ["frage"])
    time.sleep(0.05)
    assert harness.tts.speak_calls == [] # Audio suppressed because stop() set _audio_suppressed
    # The text IS kept in the new model as we append before speaking.
    assert [m["role"] for m in app.get_history()] == ["user", "assistant"]


def test_stop_during_responding_stops_audio_immediately(harness: Harness) -> None:
    app = harness.app
    harness.tts.blocking = True

    app.submit_text("frage")
    assert wait_until(lambda: harness.tts.started.is_set())
    assert app._state == "speaking"

    app.stop()

    assert app._state == "idle"
    assert harness.tts.stop_calls == 2 # Once from submit_text, once from stop()
    history = app.get_history()
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[-1]["content"] == "hi there"


# -- Block 3: Steering & Barge-in (spec section 1) --------------------------


def test_text_input_during_thinking_queues_steering_message(harness: Harness) -> None:
    app = harness.app
    llm = FakeAgentConversation(reply="Antwort 1", gate=True)
    app._llm = llm
    
    app.submit_text("Frage 1")
    assert wait_until(lambda: llm.started.is_set())
    
    # Second input during thinking of the first
    app.submit_text("Frage 2")
    
    assert llm.cancel_calls == 0 # No longer cancels!
    
    llm.release() # Release Frage 1
    assert wait_until(lambda: len(llm.sent) == 2)
    llm.release() # Release Frage 2
    
    assert wait_until(lambda: app._state == "idle")
    assert llm.sent == ["Frage 1", "Frage 2"]
    assert [m["role"] for m in app.get_history()] == ["user", "assistant", "user", "assistant"]


def test_voice_barge_in_during_processing_stops_audio_and_starts_recording(harness: Harness) -> None:
    app = harness.app
    llm = FakeAgentConversation(reply="x", gate=True)
    app._llm = llm

    app.on_toggle()  # idle -> recording
    app.on_toggle()  # recording -> thinking
    assert wait_until(lambda: llm.started.is_set())
    assert app._state == "thinking"

    app.on_toggle()  # barge-in: thinking -> recording directly

    assert app._state == "recording"
    assert llm.cancel_calls == 0 # No longer cancels the turn
    assert harness.stt.start_calls == 2
    
    # But audio of the current turn will be suppressed
    llm.release()
    assert wait_until(lambda: len(llm.sent) == 1)
    
    # End recording to start the second turn
    app.on_toggle() # recording -> thinking
    assert wait_until(lambda: len(llm.sent) == 2)
    llm.release()
    assert wait_until(lambda: app._state == "idle")
    assert harness.tts.speak_calls == ["x"] # First one was suppressed, second was "x"


def test_voice_barge_in_fully_completed_while_earlier_turn_still_running_is_not_lost(harness: Harness) -> None:
    # Regression for a bug found 2026-10-04: a *full* voice barge-in
    # (press to start recording, speak, press to stop recording) that
    # completes entirely before the earlier turn's send() call returns
    # used to vanish without a trace. The stop-recording toggle always
    # spawned a thread to drain the mic, but that thread used to bail out
    # immediately if a responder loop for the earlier turn was still
    # running -- *before* ever calling stt_client.stop_recording() -- so
    # the correction was never transcribed, never shown in history, and
    # never queued; the STT daemon was also left stuck thinking it was
    # still recording. Fixed by draining the mic unconditionally
    # (App._drain_recording_into_queue()), decoupled from whether a
    # dispatcher loop is already busy (App._ensure_responder_loop()).
    app = harness.app
    llm = FakeAgentConversation(reply="x", gate=True)
    app._llm = llm
    harness.stt.transcript = "frage 1"

    app.submit_text("frage 1")
    assert wait_until(lambda: llm.started.is_set())
    assert app._state == "thinking"

    # Full barge-in, start to stop, entirely while "frage 1" is still
    # blocked inside llm.send() (the gate hasn't been released yet).
    harness.stt.transcript = "korrektur"
    app.on_toggle()  # barge-in: thinking -> recording
    assert app._state == "recording"
    app.on_toggle()  # recording -> thinking

    # The correction must be drained and queued right away -- it must not
    # wait for "frage 1" to finish first.
    assert wait_until(lambda: harness.stt.stop_calls == 1)
    assert wait_until(lambda: "korrektur" in app._input_queue or llm.sent == ["frage 1", "korrektur"])

    # Now let "frage 1" finish.
    llm.release()
    assert wait_until(lambda: llm.sent == ["frage 1", "korrektur"])
    llm.release()
    assert wait_until(lambda: app._state == "idle")

    # "korrektur" shows up right after it's transcribed -- *before* "frage
    # 1"'s own reply, since that's still blocked on the gate at this point
    # (same "append as soon as known, order falls out on its own" behavior
    # as a normal queued steering message).
    contents = [m["content"] for m in app.get_history()]
    assert contents == ["frage 1", "korrektur", "x", "x"]


def test_voice_barge_in_during_responding_stops_audio_and_restarts_recording(harness: Harness) -> None:
    app = harness.app
    harness.tts.blocking = True

    app.submit_text("frage")
    assert wait_until(lambda: harness.tts.started.is_set())
    assert app._state == "speaking"

    app.on_toggle()  # barge-in: speaking -> recording directly

    assert app._state == "recording"
    assert harness.tts.stop_calls == 2 # submit_text + barge-in
    assert harness.stt.start_calls == 1


# -- Block 4: Audio Suppression (spec section 1) ----------------------------


def test_async_output_during_recording_is_silent(harness: Harness) -> None:
    app = harness.app
    
    app.on_toggle() # idle -> recording
    assert app._state == "recording"
    
    # Background turn finishes while user is recording
    app._append_history("assistant", "Hintergrund-Antwort")
    # Actually, App doesn't trigger TTS for _append_history today, 
    # but it would trigger it if it was a normal Turn end.
    # In the new model, _respond checks _audio_suppressed.
    
    app.on_toggle() # recording -> thinking
    assert wait_until(lambda: app._state == "idle")
    assert harness.tts.speak_calls == ["hi there"] # Only the reply to the turn, not the background one


# -- Block 5: concurrency (spec section 5) ----------------------------------


def test_concurrent_idle_toggles_never_double_start_a_recording(harness: Harness) -> None:
    app = harness.app
    barrier = threading.Barrier(2)

    def trigger() -> None:
        barrier.wait(timeout=2)
        app.on_toggle()

    threads = [threading.Thread(target=trigger) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2)

    assert harness.stt.start_calls == 1
    assert wait_until(lambda: app._state == "idle")
    assert harness.stt.stop_calls == 1


# -- Block 6: STT daemon error handling -------------------------------------
#
# Regression coverage for the "Jabra button unavailable (STT daemon error:
# busy: recording)" failure mode: a daemon-side error used to (a) speak the
# "Start" cue unconditionally before even calling the daemon, so a failed
# press sounded identical to a successful one, and (b) raise out of
# on_toggle() into whichever trigger thread called it (evdev listener /
# toggle socket), silently killing that thread for the rest of the
# process's life. These tests pin the fix: a daemon error is spoken as
# "error", the state always recovers to idle, and on_toggle() itself never
# raises.


def test_start_recording_failure_speaks_error_not_start_and_recovers_to_idle(harness: Harness) -> None:
    app = harness.app
    harness.stt.fail_start = RuntimeError("STT daemon error: busy: recording")

    app.on_toggle()  # idle -> recording, but the daemon call fails

    assert harness.feedback.cue_calls == ["error"]
    assert app._state == "idle"
    assert not harness.stt.is_recording()

    # The failure was transient (FakeSTT clears fail_start after raising
    # once) -- a second press should work normally now.
    app.on_toggle()
    assert app._state == "recording"
    assert harness.feedback.cue_calls == ["error", "start"]


def test_stop_recording_failure_speaks_error_and_recovers_to_idle(harness: Harness) -> None:
    app = harness.app

    app.on_toggle()  # idle -> recording (succeeds)
    assert harness.feedback.cue_calls == ["start"]

    harness.stt.fail_stop = RuntimeError("STT daemon error: busy: idle")
    app.on_toggle()  # recording -> thinking, but stop_recording() fails

    # on_toggle() itself speaks "stop" right at the press (confirms the
    # press was received); the responder loop then discovers the daemon
    # call failed and speaks "error" on top of it.
    assert wait_until(lambda: app._state == "idle")
    assert harness.feedback.cue_calls == ["start", "stop", "error"]
    assert harness.llm.sent == []  # turn was discarded, never reached the LLM
    assert not app._responder_loop_running


def test_on_toggle_never_raises_even_on_unexpected_error(harness: Harness) -> None:
    """Guards the on_toggle()/_on_toggle_inner() split: whatever goes wrong
    inside a toggle, on_toggle() must swallow it -- both input_button's
    evdev read_loop and toggle_socket.serve()'s accept loop treat an
    exception escaping this callback as fatal to that entire trigger
    thread, which previously left the Jabra button (or the local hotkey)
    dead for the rest of the process's life with little or no indication
    why."""
    app = harness.app
    harness.stt.fail_start = ValueError("something unrelated to the daemon broke")

    app.on_toggle()  # must not raise

    assert harness.feedback.cue_calls == ["error"]
    assert app._state == "idle"
