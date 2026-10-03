"""Tests for the background-result channel (docs/specs/background-channel.md):
a Claude backgroundable task reporting back on its own, with nobody waiting
on a reply at the moment it finishes.

Two layers, tested separately (same split test_output_categories.py uses for
the response/thinking/other categorization):

- llm.py's `_route()`/`_deliver_background()`: which message types count as
  "the task is done", and whether that goes to the active turn's queue or to
  `on_background_result` -- tested here directly against a bare
  `ClaudeCodeConversation` instance (`object.__new__`, no real SDK
  connection) fed real `claude_agent_sdk` dataclasses. No API key or network
  needed: these are plain dataclasses, and `_route()` itself does nothing
  async.
- `App.deliver_background_result()`'s queueing/delivery rules -- tested
  through the existing Harness/FakeAgentConversation fixtures, same as
  test_output_categories.py and test_background_and_status_gaps.py.
"""

from __future__ import annotations

import asyncio

from claude_agent_sdk import (
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
)

from speech_to_speech import app as app_module
from speech_to_speech.llm import ClaudeCodeConversation, _describe_task_message

from .conftest import Harness, wait_until
from .fakes import FakeAgentConversation

# -- Helpers for the llm.py layer -------------------------------------------


def _task_started(task_id: str = "t1", description: str = "Build läuft") -> TaskStartedMessage:
    return TaskStartedMessage(
        subtype="task_started", data={}, task_id=task_id, description=description, uuid="u1", session_id="s1"
    )


def _task_progress(task_id: str = "t1", description: str = "Build läuft") -> TaskProgressMessage:
    return TaskProgressMessage(
        subtype="task_progress",
        data={},
        task_id=task_id,
        description=description,
        usage={"total_tokens": 0, "tool_uses": 0, "duration_ms": 0},
        uuid="u2",
        session_id="s1",
    )


def _task_updated(task_id: str = "t1", status: str = "completed") -> TaskUpdatedMessage:
    return TaskUpdatedMessage(subtype="task_updated", data={}, task_id=task_id, patch={"status": status}, status=status)


def _task_notification(
    task_id: str = "t1", status: str = "completed", summary: str = "Build erfolgreich."
) -> TaskNotificationMessage:
    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status=status,
        output_file="/tmp/out.txt",
        summary=summary,
        uuid="u3",
        session_id="s1",
    )


def _bare_conversation(on_background_result=None) -> ClaudeCodeConversation:
    """A ClaudeCodeConversation with none of __init__'s real connection work
    done -- just the attributes _route()/_deliver_background() touch, so
    this is testable without a live SDK client or API key."""
    conv = object.__new__(ClaudeCodeConversation)
    conv._active_tasks = {}
    conv._turn_queue = None
    conv._on_background_result = on_background_result
    return conv


# -- llm.py: _route() / _deliver_background() -------------------------------


def test_route_terminal_notification_while_idle_delivers_background_result() -> None:
    calls: list[tuple[str, str]] = []
    conv = _bare_conversation(on_background_result=lambda source, text: calls.append((source, text)))

    conv._route(_task_started(description="Build läuft"))
    conv._route(_task_notification(summary="Build erfolgreich."))

    assert calls == [("Build läuft", "Build erfolgreich.")]
    # Bookkeeping cleared once the task reached a terminal status.
    assert conv._active_tasks == {}


def test_route_falls_back_to_description_when_notification_has_no_summary() -> None:
    calls: list[tuple[str, str]] = []
    conv = _bare_conversation(on_background_result=lambda source, text: calls.append((source, text)))

    conv._route(_task_started(description="Aufräumen"))
    conv._route(_task_notification(summary="", status="failed"))

    assert calls == [("Aufräumen", "Aufräumen (failed)")]


def test_route_with_turn_in_flight_goes_to_turn_queue_not_background() -> None:
    # The core "don't deliver through the background channel if a normal
    # send() call is already going to see this message" rule.
    calls: list[tuple[str, str]] = []
    conv = _bare_conversation(on_background_result=lambda source, text: calls.append((source, text)))
    conv._turn_queue = asyncio.Queue()

    msg = _task_notification()
    conv._route(msg)

    assert calls == []
    assert conv._turn_queue.get_nowait() is msg
    # Bookkeeping still runs regardless of where the message was routed.
    assert conv._active_tasks == {}


def test_route_non_terminal_messages_while_idle_are_not_delivered() -> None:
    calls: list[tuple[str, str]] = []
    conv = _bare_conversation(on_background_result=lambda source, text: calls.append((source, text)))

    conv._route(_task_started(description="Build läuft"))
    conv._route(_task_progress(description="Build läuft"))

    assert calls == []
    # Still tracked -- the task hasn't reached a terminal status yet.
    assert conv._active_tasks == {"t1": "Build läuft"}


def test_route_task_updated_with_non_terminal_status_is_not_delivered() -> None:
    calls: list[tuple[str, str]] = []
    conv = _bare_conversation(on_background_result=lambda source, text: calls.append((source, text)))

    conv._route(_task_started())
    conv._route(_task_updated(status="running"))

    assert calls == []
    assert conv._active_tasks == {"t1": "Build läuft"}


def test_route_without_on_background_result_callback_is_a_noop() -> None:
    conv = _bare_conversation(on_background_result=None)

    conv._route(_task_started())
    conv._route(_task_notification())  # must not raise

    assert conv._active_tasks == {}


def test_describe_task_message_formats_each_type() -> None:
    assert "Build läuft" in _describe_task_message(_task_started(description="Build läuft"))
    assert "completed" in _describe_task_message(_task_updated(status="completed"))
    assert "Build erfolgreich." in _describe_task_message(_task_notification(summary="Build erfolgreich."))


# -- App.deliver_background_result() -----------------------------------------


def test_deliver_background_result_while_idle_speaks_it_and_tags_the_source(harness: Harness) -> None:
    app = harness.app
    assert app._state == "idle"

    app.deliver_background_result("Build-Agent", "Build ist fertig.")

    # Note: _state is "idle" both before this call *and* again once the
    # spawned responder-loop thread finishes -- waiting on that alone would
    # race (it can read as "idle" before the thread has done anything).
    # History actually gaining an entry is the real completion signal.
    assert wait_until(lambda: len(app.get_history()) == 1)
    assert app._state == "idle"
    history = app.get_history()
    assert len(history) == 1
    assert history[0]["role"] == "assistant"
    assert history[0]["content"] == "Build ist fertig."
    assert "Build-Agent" in history[0]["metadata"]["title"]
    # Unlike CATEGORY_THINKING/OTHER, a background result *is* spoken --
    # it's a complete, standalone message, not an intermediate step.
    assert harness.tts.speak_calls == ["Build ist fertig."]


def test_deliver_background_result_during_active_turn_is_queued_and_flushed_after(harness: Harness) -> None:
    app = harness.app
    app._llm = FakeAgentConversation(reply="normale Antwort", gate=True)

    app.submit_text("frage")
    assert wait_until(lambda: app._llm.started.is_set())
    assert app._state == "thinking"

    app.deliver_background_result("Bg-Agent", "Ergebnis fertig.")
    # Queued, not delivered yet -- the running turn has priority.
    assert harness.tts.speak_calls == []
    assert [m["role"] for m in app.get_history()] == ["user"]

    app._llm.release()
    assert wait_until(lambda: app._state == "idle")

    contents = [m["content"] for m in app.get_history()]
    assert contents == ["frage", "normale Antwort", "Ergebnis fertig."]
    assert harness.tts.speak_calls == ["normale Antwort", "Ergebnis fertig."]


def test_deliver_background_result_during_recording_waits_for_that_turn(harness: Harness) -> None:
    app = harness.app

    app.on_toggle()  # idle -> recording
    assert app._state == "recording"

    app.deliver_background_result("Bg-Agent", "Ergebnis fertig.")
    assert harness.tts.speak_calls == []
    assert app.get_history() == []

    app.on_toggle()  # recording -> thinking: runs the recorded turn, then
    # should flush the queued background result before going idle.
    assert wait_until(lambda: app._state == "idle")

    contents = [m["content"] for m in app.get_history()]
    assert contents == ["hello", "hi there", "Ergebnis fertig."]
    assert harness.tts.speak_calls == ["hi there", "Ergebnis fertig."]


def test_load_wires_on_background_result_to_deliver_background_result(harness: Harness, monkeypatch) -> None:
    app = harness.app
    captured_kwargs: dict[str, object] = {}

    def fake_create_conversation(agent, **kwargs):
        captured_kwargs.update(kwargs)
        return FakeAgentConversation()

    monkeypatch.setattr(app_module, "create_conversation", fake_create_conversation)

    app.load()

    assert captured_kwargs.get("on_background_result") == app.deliver_background_result


def test_reset_wires_on_background_result_to_deliver_background_result(harness: Harness, monkeypatch) -> None:
    app = harness.app
    captured_kwargs: dict[str, object] = {}

    def fake_create_conversation(agent, **kwargs):
        captured_kwargs.update(kwargs)
        return FakeAgentConversation()

    monkeypatch.setattr(app_module, "create_conversation", fake_create_conversation)

    app.reset()

    assert captured_kwargs.get("on_background_result") == app.deliver_background_result
