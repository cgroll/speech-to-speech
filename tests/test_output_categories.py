"""Tests for the output-categorization feature (response/thinking/other,
docs/specs/thinking-channel-and-stop-marker.md section 3a): App._append_output(),
that create_conversation() is actually wired up with it, and the one
safety-critical guarantee the whole feature depends on -- that TTS never
sees anything but the final response, no matter what else streamed by on
the way there.

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
    assert entry["metadata"]["status"] == "done"


def test_append_output_tags_other_with_its_own_title(harness: Harness) -> None:
    app = harness.app

    app._append_output(CATEGORY_OTHER, "→ bash(ls)")

    entry = app.get_history()[0]
    assert entry["metadata"]["title"] == app_module.OUTPUT_CATEGORY_TITLES[CATEGORY_OTHER]


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


# -- The safety-critical guarantee: voice only ever gets the response -------


def test_thinking_output_during_a_turn_never_reaches_tts(harness: Harness) -> None:
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
    # Both categorized chunks landed in the chat history...
    roles_and_content = [(m["role"], m["content"]) for m in app.get_history()]
    assert roles_and_content == [
        ("user", "frage"),
        ("assistant", "erstmal nachdenken..."),
        ("assistant", "→ bash(ls)"),
        ("assistant", "die kurze, gesprochene Antwort"),
    ]
    # ...but only the response -- the last entry above -- ever went to TTS.
    assert harness.tts.speak_calls == ["die kurze, gesprochene Antwort"]


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
