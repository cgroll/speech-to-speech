from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from speech_to_speech import app as app_module
from speech_to_speech.app import App

from .fakes import FakeAgentConversation, FakeFeedback, FakeSTT, FakeTTS


def wait_until(predicate, timeout: float = 2.0, interval: float = 0.01) -> bool:
    """Polls `predicate` until it's true or `timeout` elapses -- the
    deterministic alternative to a fixed sleep for waiting on App's
    background turn-handler threads (on_toggle()/submit_text() both hand
    the actual LLM/TTS work off to a daemon thread; see app.py). Returns
    the final predicate value so a timed-out wait still fails the assertion
    that uses it, rather than silently passing."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@dataclass
class Harness:
    app: App
    llm: FakeAgentConversation
    stt: FakeSTT
    tts: FakeTTS
    feedback: FakeFeedback


@pytest.fixture
def harness(monkeypatch) -> Harness:
    """A real App wired to fakes instead of real daemons/LLM -- skips
    App.load() (which talks to real STT/TTS daemons and constructs a real
    backend) entirely. monkeypatches the stt_client/tts_client/feedback
    *module objects* app.py holds references to, so every `stt_client.foo()`
    / `tts_client.foo()` / `feedback.play_cue()` call inside app.py
    transparently hits the fake -- feedback in particular needs this so the
    suite doesn't actually shell out to espeak-ng/say on every on_toggle()."""
    stt = FakeSTT()
    tts = FakeTTS()
    fb = FakeFeedback()
    monkeypatch.setattr(app_module, "stt_client", stt)
    monkeypatch.setattr(app_module, "tts_client", tts)
    monkeypatch.setattr(app_module, "feedback", fb)

    app = App()
    llm = FakeAgentConversation(reply="hi there")
    app._llm = llm

    return Harness(app=app, llm=llm, stt=stt, tts=tts, feedback=fb)
