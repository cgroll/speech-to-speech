"""A dead/misbehaving TTS daemon must not wedge the dialog loop: a typed
prompt still gets processed, the worker survives any speak() failure, Stop
still cancels the agent, and resend() un-sticks a stalled queue."""

from __future__ import annotations

from speech_to_speech.tts_client import DaemonUnavailableError

from .conftest import Harness, wait_until


def _dead_tts(harness: Harness) -> None:
    def boom(*_a, **_k):
        raise DaemonUnavailableError("TTS daemon not running")

    harness.tts.stop = boom  # type: ignore[method-assign]
    harness.tts.speak = boom  # type: ignore[method-assign]


def test_prompt_is_processed_when_tts_daemon_is_down(harness: Harness) -> None:
    _dead_tts(harness)
    harness.app.submit_text("hallo")
    assert wait_until(lambda: harness.app._state == "idle" and harness.app._stats["turns"] == 1)
    assert harness.app._input_queue == []


def test_tts_worker_survives_unexpected_exception(harness: Harness) -> None:
    def boom(_text):
        raise TimeoutError("timed out")

    harness.tts.speak = boom  # type: ignore[method-assign]
    harness.app.submit_text("eins")
    assert wait_until(lambda: harness.app._state == "idle" and harness.app._stats["turns"] == 1)
    harness.app.submit_text("zwei")
    assert wait_until(lambda: harness.app._state == "idle" and harness.app._stats["turns"] == 2)


def test_stop_cancels_agent_even_if_tts_daemon_is_down(harness: Harness) -> None:
    _dead_tts(harness)
    harness.app.stop()
    assert harness.app._state == "idle"


def test_resend_kicks_stalled_queue(harness: Harness) -> None:
    app = harness.app
    app._input_queue.append("haengt")
    app._append_history("user", "haengt")
    msg = app.resend()
    assert "verarbeitet" in msg
    assert wait_until(lambda: app._state == "idle" and app._stats["turns"] == 1)


def test_resend_resubmits_last_user_message(harness: Harness) -> None:
    app = harness.app
    app.submit_text("frage")
    assert wait_until(lambda: app._state == "idle" and app._stats["turns"] == 1)
    assert "erneut" in app.resend()
    assert wait_until(lambda: app._state == "idle" and app._stats["turns"] == 2)
    assert sum(1 for m in app._history if m["role"] == "user") == 1
