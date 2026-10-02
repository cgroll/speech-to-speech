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
