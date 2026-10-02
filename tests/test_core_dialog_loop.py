"""Tests for the state model in docs/specs/core-dialog-loop.md, exercised
against the real App (src/speech_to_speech/app.py) with fakes standing in
for the LLM backend and the STT/TTS daemons (see tests/fakes.py).

App's internal state names are idle/recording/thinking/speaking rather than
the spec's Idle/Processing/Responding -- recording+thinking together are
voice's Processing (capture, then the agent call), thinking alone is text's
Processing (no capture phase), speaking is Responding. Each test says in a
comment which spec section it checks.

Structured in the four blocks discussed before writing these: plain
transitions, Stop, Barge-in, concurrency/fresh-interrupt-signal.
"""

from __future__ import annotations

import threading
import time

from speech_to_speech.agent_backend import LlmTimeoutError
from speech_to_speech.app import TIMEOUT_MESSAGE

from .conftest import Harness, wait_until
from .fakes import FakeAgentConversation

# -- Block 1: plain transitions (spec section 3/4) -------------------------


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

    app.on_toggle()  # recording -> thinking (spawns the turn-handler thread)

    assert wait_until(lambda: app._state == "idle")
    assert harness.stt.stop_calls == 1
    assert harness.llm.sent == ["hello"]  # FakeSTT's default transcript
    assert harness.tts.speak_calls == ["hi there"]


def test_processing_state_is_visible_while_agent_call_is_pending(harness: Harness) -> None:
    # Spec section 6: Processing must be observably its own state, not just
    # an instant in text-only mode.
    app = harness.app
    app._llm = FakeAgentConversation(reply="later", gate=True)

    app.submit_text("frage")
    assert wait_until(lambda: app._llm.started.is_set())
    assert app._state == "thinking"
    assert app.get_state() == "Denkt nach…"

    app._llm.release()
    assert wait_until(lambda: app._state == "idle")


def test_reply_is_shown_even_when_voice_output_is_muted(harness: Harness) -> None:
    # Spec section 4: "Text wird in jedem Fall angezeigt" is a hard
    # requirement, independent of whether a voice adapter is attached/muted.
    app = harness.app
    app.set_voice_muted(True)

    app.submit_text("frage")

    assert wait_until(lambda: app._state == "idle")
    assert [m["role"] for m in app.get_history()] == ["user", "assistant"]
    assert harness.tts.speak_calls == []


def test_llm_timeout_recovers_to_idle_with_a_spoken_message(harness: Harness) -> None:
    # Robustness item from docs/specification.md ("Aktivitätsbasierter
    # Timeout") rather than core-dialog-loop.md itself, but same plumbing.
    app = harness.app
    app._llm = FakeAgentConversation(raises=LlmTimeoutError("stuck"))

    app.submit_text("frage")

    assert wait_until(lambda: app._state == "idle")
    assert app.get_history()[-1]["content"] == TIMEOUT_MESSAGE
    assert harness.tts.speak_calls == [TIMEOUT_MESSAGE]


# -- Block 2: Stop (spec section 4, "Stop") ---------------------------------


def test_stop_during_processing_discards_reply_without_speaking(harness: Harness) -> None:
    # Antwort-Kanal noch leer: Agentenaufruf abgebrochen, keine Sprachausgabe.
    app = harness.app
    app._llm = FakeAgentConversation(reply="too late", gate=True)

    app.submit_text("frage")
    assert wait_until(lambda: app._llm.started.is_set())
    assert app._state == "thinking"

    app.stop()

    assert app._state == "idle"
    assert app._llm.cancel_calls == 1
    # cancel() released the gated send(); give the turn-handler thread a
    # moment to run its (now-interrupted) discard path.
    assert wait_until(lambda: app._llm.sent == ["frage"])
    time.sleep(0.05)
    assert harness.tts.speak_calls == []
    assert [m["role"] for m in app.get_history()] == ["user"]


def test_stop_during_responding_keeps_text_and_stops_audio_only(harness: Harness) -> None:
    # Antwort-Kanal schon voll: Text bleibt, keine Markierung, nur TTS stoppt.
    app = harness.app
    harness.tts.blocking = True

    app.submit_text("frage")
    assert wait_until(lambda: harness.tts.started.is_set())
    assert app._state == "speaking"

    app.stop()

    assert app._state == "idle"
    assert harness.tts.stop_calls == 1
    history = app.get_history()
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[-1]["content"] == "hi there"  # unchanged, no abort marker


# -- Block 3: Barge-in (spec section 4, "Barge-in") -------------------------


def test_voice_barge_in_during_processing_skips_idle(harness: Harness) -> None:
    app = harness.app
    app._llm = FakeAgentConversation(reply="x", gate=True)

    app.on_toggle()  # idle -> recording
    app.on_toggle()  # recording -> thinking
    assert wait_until(lambda: app._llm.started.is_set())
    assert app._state == "thinking"
    old_interrupt = app._interrupt

    app.on_toggle()  # barge-in: thinking -> recording directly, no idle hop

    assert app._state == "recording"
    assert old_interrupt.is_set()
    assert app._llm.cancel_calls == 1
    assert harness.stt.start_calls == 2  # original turn's + the barge-in's


def test_voice_barge_in_during_responding_stops_audio_and_restarts_recording(harness: Harness) -> None:
    app = harness.app
    harness.tts.blocking = True

    app.submit_text("frage")
    assert wait_until(lambda: harness.tts.started.is_set())
    assert app._state == "speaking"

    app.on_toggle()  # barge-in: speaking -> recording directly

    assert app._state == "recording"
    assert harness.tts.stop_calls == 1
    assert harness.stt.start_calls == 1


def test_text_barge_in_during_processing_uses_a_fresh_interrupt_event(harness: Harness) -> None:
    app = harness.app
    app._llm = FakeAgentConversation(reply="x", gate=True)

    app.submit_text("erste frage")
    assert wait_until(lambda: app._llm.started.is_set())
    old_interrupt = app._interrupt

    app.submit_text("zweite frage")  # barge-in: no idle hop, new text is the next input directly

    assert app._state == "thinking"
    assert old_interrupt.is_set()
    assert app._interrupt is not old_interrupt
    assert not app._interrupt.is_set()
    assert app._llm.cancel_calls == 1
    assert app._llm.sent[-1] == "zweite frage"


# -- Block 4: concurrency / fresh-interrupt-signal (spec section 5) --------


def test_concurrent_idle_toggles_never_double_start_a_recording(harness: Harness) -> None:
    # Two trigger sources (Jabra button, local hotkey) can call on_toggle()
    # at the same instant; the short state_lock must still serialize them
    # into exactly one "start" rather than racing on a torn read of
    # self._state.
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

    # The lock guarantees a strict order even though both calls were
    # concurrent: one claims idle->recording, the other then necessarily
    # sees "recording" and claims the stop-and-respond half of the toggle --
    # a real (if fast) double-press, not a race. Either way, start_recording
    # must fire exactly once.
    assert harness.stt.start_calls == 1
    assert wait_until(lambda: app._state == "idle")
    assert harness.stt.stop_calls == 1


def test_each_turn_gets_a_fresh_interrupt_event_even_without_barge_in(harness: Harness) -> None:
    app = harness.app

    app.submit_text("erste")
    assert wait_until(lambda: app._state == "idle")
    first_event = app._interrupt

    app.submit_text("zweite")
    assert wait_until(lambda: app._state == "idle")
    second_event = app._interrupt

    assert first_event is not second_event
    assert not second_event.is_set()


def test_each_voice_turn_gets_a_fresh_interrupt_event_even_without_barge_in(harness: Harness) -> None:
    # Same guarantee as above, through the voice path (on_toggle's
    # recording -> thinking leg) rather than submit_text()'s -- the two
    # create their fresh Event in different places in app.py, so this is
    # not redundant with the text-mode test.
    app = harness.app

    app.on_toggle()  # idle -> recording
    app.on_toggle()  # recording -> thinking
    assert wait_until(lambda: app._state == "idle")
    first_event = app._interrupt

    app.on_toggle()
    app.on_toggle()
    assert wait_until(lambda: app._state == "idle")
    second_event = app._interrupt

    assert first_event is not second_event
    assert not second_event.is_set()
