"""Tests for the output-categorization feature (response/thinking/other,
docs/specs/thinking-channel-and-stop-marker.md section 3a): App._append_output(),
that create_conversation() is actually wired up with it, and the
safety-critical guarantee the feature still depends on -- that TTS never
sees raw CATEGORY_OTHER (tool call/result) noise. CATEGORY_THINKING is
*not* silent: since docs/specs/tts-output-queue.md it's deliberately spoken
too, through the same ordered queue as the final response (see
test_tts_output_queue.py for that ordering guarantee).

The block-classification logic itself (which block/event type counts as
which category) lives inside llm.py's async Claude SDK loop and
pi_agent.py's subprocess JSON parsing -- not unit-tested here, that would
need a full mock of the SDK's async client or pi's stdout. Verified instead
with real end-to-end smoke tests against both live backends (see the commit
that added this). This file covers everything on App's side of the
on_output(category, text) callback boundary."""

from __future__ import annotations

from speech_to_speech import app as app_module
from speech_to_speech.agent_backend import CATEGORY_OTHER, CATEGORY_THINKING

from .conftest import Harness, wait_until
from .fakes import FakeAgentConversation

# -- App._append_output() in isolation --------------------------------------


def test_append_output_tags_thinking_with_gradio_metadata(harness: Harness) -> None:
    app = harness.app

    app._append_output(CATEGORY_THINKING, "hmm, let me check that")

    history = app.get_history()
    assert len(history) == 1
    entry = history[0]
    assert entry["role"] == "assistant"
    assert entry["content"] == "hmm, let me check that"
    assert entry["metadata"]["title"] == app_module.OUTPUT_CATEGORY_TITLES[CATEGORY_THINKING]
    # 2026-10-02: deliberately no "status" key -- Gradio only starts a
    # thought bubble collapsed when status="done" is present, so omitting
    # it makes thinking output show up already expanded (see _append_output()'s
    # docstring/inline comment in app.py).
    assert "status" not in entry["metadata"]


def test_append_output_filters_other_from_history_but_logs_it(harness: Harness, caplog) -> None:
    # 2026-10-02: CATEGORY_OTHER (tool calls/results) is deliberately kept
    # out of the chat history to avoid cluttering the UI -- it's only
    # surfaced via logging, for developers watching the terminal (see
    # docs/channels.md and _append_output()'s docstring in app.py).
    app = harness.app

    with caplog.at_level("INFO"):
        app._append_output(CATEGORY_OTHER, "→ bash(ls)")

    assert app.get_history() == []
    assert "→ bash(ls)" in caplog.text


def test_append_output_falls_back_to_a_generic_title_for_an_unknown_category(harness: Harness) -> None:
    # The whole point of CATEGORY_OTHER being a catch-all is that future,
    # still-unnamed categories shouldn't crash this -- a backend handing in
    # some category string we've never seen must still degrade gracefully.
    app = harness.app

    app._append_output("mystery_category", "???")

    entry = app.get_history()[0]
    assert "mystery_category" in entry["metadata"]["title"]


def test_normal_history_entries_carry_no_metadata(harness: Harness) -> None:
    # Gradio only renders the "thought" bubble styling when `metadata` is
    # present -- a plain user/assistant turn must not accidentally get one.
    app = harness.app

    app._append_history("user", "hallo")
    app._append_history("assistant", "hi")

    for entry in app.get_history():
        assert "metadata" not in entry


# -- Wiring: create_conversation() actually receives on_output --------------


def test_load_wires_on_output_to_append_output(harness: Harness, monkeypatch) -> None:
    app = harness.app
    captured_kwargs: dict[str, object] = {}

    def fake_create_conversation(agent, **kwargs):
        captured_kwargs.update(kwargs)
        return FakeAgentConversation()

    monkeypatch.setattr(app_module, "create_conversation", fake_create_conversation)

    app.load()

    assert captured_kwargs.get("on_output") == app._append_output


def test_reset_wires_on_output_to_append_output(harness: Harness, monkeypatch) -> None:
    app = harness.app
    captured_kwargs: dict[str, object] = {}

    def fake_create_conversation(agent, **kwargs):
        captured_kwargs.update(kwargs)
        return FakeAgentConversation()

    monkeypatch.setattr(app_module, "create_conversation", fake_create_conversation)

    app.reset()

    assert captured_kwargs.get("on_output") == app._append_output


# -- The safety-critical guarantee: voice never gets raw tool-call noise ----
#
# 2026-10-03 (docs/specs/tts-output-queue.md): thinking chunks are no longer
# silent -- they're deliberately spoken too, through the same ordered TTS
# queue as the final response (see test_tts_output_queue.py for the
# ordering guarantee itself). The guarantee that still holds, and that this
# test pins, is narrower: CATEGORY_OTHER (raw tool calls/results) must never
# reach TTS, only CATEGORY_THINKING and the final response may.


def test_tool_call_output_never_reaches_tts_but_thinking_and_response_do(harness: Harness) -> None:
    app = harness.app
    app._llm = FakeAgentConversation(
        reply="die kurze, gesprochene Antwort",
        on_output=app._append_output,
        emits=[
            (CATEGORY_THINKING, "erstmal nachdenken..."),
            (CATEGORY_OTHER, "→ bash(ls)"),
        ],
    )

    app.submit_text("frage")

    assert wait_until(lambda: app._state == "idle")
    # The thinking chunk lands in the chat history; the CATEGORY_OTHER (tool
    # call) chunk is deliberately filtered out of the UI (logged instead,
    # see test_append_output_filters_other_from_history_but_logs_it)...
    roles_and_content = [(m["role"], m["content"]) for m in app.get_history()]
    assert roles_and_content == [
        ("user", "frage"),
        ("assistant", "erstmal nachdenken..."),
        ("assistant", "die kurze, gesprochene Antwort"),
    ]
    # ...and while the thinking chunk and the response both get spoken (in
    # order), the tool-call chunk never does.
    assert harness.tts.speak_calls == ["erstmal nachdenken...", "die kurze, gesprochene Antwort"]


def test_thinking_output_still_shown_as_text_even_when_voice_is_muted(harness: Harness) -> None:
    # The core-dialog-loop.md hard rule ("Text wird in jedem Fall
    # angezeigt") applies to categorized output too, not just the response.
    app = harness.app
    app.set_voice_muted(True)
    app._llm = FakeAgentConversation(
        reply="Antwort",
        on_output=app._append_output,
        emits=[(CATEGORY_THINKING, "Gedanke")],
    )

    app.submit_text("frage")

    assert wait_until(lambda: app._state == "idle")
    contents = [m["content"] for m in app.get_history()]
    assert contents == ["frage", "Gedanke", "Antwort"]
    assert harness.tts.speak_calls == []
