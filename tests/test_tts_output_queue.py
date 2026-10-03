"""Tests for docs/specs/tts-output-queue.md: a thinking-chunk and the final
response must both reach TTS (not race each other for the daemon's single
busy slot, see the bug described in that spec's section 1), and a barge-in
must purge anything still waiting in `_tts_queue` instead of letting it
trickle out after the user has already moved on (section 4)."""

from __future__ import annotations

from speech_to_speech.agent_backend import CATEGORY_THINKING

from .conftest import Harness, wait_until
from .fakes import FakeAgentConversation


def test_thinking_chunk_and_final_response_are_both_spoken_in_order(harness: Harness) -> None:
    app = harness.app
    app._llm = FakeAgentConversation(
        reply="die Antwort",
        on_output=app._append_output,
        emits=[(CATEGORY_THINKING, "erstmal nachdenken")],
    )

    app.submit_text("frage")

    assert wait_until(lambda: app._state == "idle")
    # Both the thinking chunk and the final response were actually sent to
    # the TTS daemon, in the order they were produced -- neither lost a race
    # for the daemon's single busy slot (the bug this queue fixes).
    assert harness.tts.speak_calls == ["erstmal nachdenken", "die Antwort"]


def test_barge_in_purges_queued_but_not_yet_played_thinking_chunk(harness: Harness) -> None:
    app = harness.app
    harness.tts.blocking = True  # first speak() call blocks until stop()
    app._llm = FakeAgentConversation(
        reply="die Antwort",
        on_output=app._append_output,
        emits=[(CATEGORY_THINKING, "chunk1"), (CATEGORY_THINKING, "chunk2")],
        gate=True,  # keep send() from returning the final reply yet
    )

    app.submit_text("frage")

    # The worker has picked up chunk1 and is blocked inside speak() for it;
    # chunk2 is still waiting, un-consumed, in _tts_queue.
    assert wait_until(lambda: harness.tts.speak_calls == ["chunk1"])
    assert wait_until(lambda: len(app._tts_queue) == 1)

    app.on_toggle()  # barge-in: thinking -> recording

    assert app._state == "recording"
    # chunk2 must never reach the daemon, whether it gets dropped by the
    # purge itself or by the worker's own skip-check -- see this test file's
    # module docstring and tts-output-queue.md section 4.
    assert wait_until(lambda: app._tts_queue == [])
    assert harness.tts.speak_calls == ["chunk1"]
