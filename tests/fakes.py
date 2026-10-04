"""Test doubles for App's external collaborators -- the agent backend
(agent_backend.AgentConversation: send/cancel/close) and the STT/TTS daemon
clients (stt_client.py/tts_client.py) -- so the state-model tests in
test_core_dialog_loop.py can drive App without a real LLM, microphone, or
TTS daemon.

See docs/specs/core-dialog-loop.md for the model these are testing against.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import numpy as np


class FakeAgentConversation:
    """Stands in for ClaudeCodeConversation/PiAgentConversation behind the
    AgentConversation protocol.

    `reply` is either a fixed string, or a callable `text -> str` for tests
    where distinct calls need distinct replies (e.g. telling apart "the
    original task's answer" from "the status question's own answer" when
    both go through the same fake instance, since App reuses one backend
    across turns in a session). By default `send()` returns it immediately.
    With `gate=True`,
    every call to `send()` blocks until released -- either by the test
    calling `release()`, or by `cancel()` (a real backend's cancel() is a
    best-effort interrupt of whatever's in flight, so the fake's cancel()
    unblocks the currently pending send() the same way). A *fresh* release
    gate is created per call, so a second turn on the same fake blocks
    again even after an earlier cancel() -- matching that App reuses one
    backend instance across turns in a session.

    This is what makes "catch App in Processing, then do X" deterministic
    in tests instead of relying on sleeps/timing.

    `on_output` + `emits`: real backends take `on_output(category, text)` at
    construction (agent_backend.create_conversation(..., on_output=...)) and
    call it zero or more times *during* send(), before returning the
    response -- see llm.py/pi_agent.py. `emits` is the list of
    `(category, text)` pairs this fake calls `on_output` with, fired right
    at the start of send(), before gating -- mirrors a backend emitting its
    thinking/tool-call output before the final answer is ready.
    """

    def __init__(
        self,
        reply: str | Callable[[str], str] = "ok",
        *,
        gate: bool = False,
        raises: Exception | None = None,
        on_output: Callable[[str, str], None] | None = None,
        emits: list[tuple[str, str]] = (),
    ):
        self.reply = reply
        self.raises = raises
        self.gate = gate
        self.on_output = on_output
        self.emits = list(emits)
        self.sent: list[str] = []
        self.cancel_calls = 0
        self.close_calls = 0
        self.started = threading.Event()  # set once the *current* call is inside send()
        self._release = threading.Event()

    def send(self, text: str) -> str:
        self.started.clear()
        release = threading.Event()
        self._release = release
        self.sent.append(text)
        if self.on_output is not None:
            for category, chunk in self.emits:
                self.on_output(category, chunk)
        self.started.set()
        if self.gate:
            release.wait(timeout=5)
        if self.raises is not None:
            raise self.raises
        return self.reply(text) if callable(self.reply) else self.reply

    def cancel(self) -> None:
        self.cancel_calls += 1
        self._release.set()

    def release(self) -> None:
        self._release.set()

    def close(self) -> None:
        self.close_calls += 1


class FakeSTT:
    """speech_to_speech.stt_client stand-in.

    `fail_start`/`fail_stop` simulate the daemon answering {"ok": False,
    "error": ...} (stt_client.py raises RuntimeError for that case) --
    e.g. the "busy: recording" error that prompted app.py's
    _start_recording()/_run_responder_loop() to handle this explicitly
    instead of letting it kill the calling thread (evdev listener / toggle
    socket). Raised once, then cleared, matching a transient daemon hiccup
    rather than a permanently broken daemon."""

    def __init__(self, transcript: str = "hello"):
        self.transcript = transcript
        self.start_calls = 0
        self.stop_calls = 0
        self._is_recording = False
        self.fail_start: Exception | None = None
        self.fail_stop: Exception | None = None

    def ensure_available(self) -> None:
        pass

    def start_recording(self) -> None:
        self.start_calls += 1
        if self.fail_start is not None:
            exc, self.fail_start = self.fail_start, None
            raise exc
        self._is_recording = True

    def stop_recording(self) -> str:
        self.stop_calls += 1
        if self.fail_stop is not None:
            exc, self.fail_stop = self.fail_stop, None
            raise exc
        self._is_recording = False
        return self.transcript

    def is_recording(self) -> bool:
        return self._is_recording

    def transcribe(self, audio: np.ndarray) -> str:
        return self.transcript


class FakeTTS:
    """speech_to_speech.tts_client stand-in. With `blocking=True`, speak()
    waits until stop() is called -- mirroring the real daemon, where
    tts_client.speak() blocks until playback finishes or a barge-in's
    tts_client.stop() cancels it (see app.py's _speak())."""

    def __init__(self, blocking: bool = False):
        self.blocking = blocking
        self.speak_calls: list[str] = []
        self.stop_calls = 0
        self.started = threading.Event()
        self._release = threading.Event()

    def ensure_available(self) -> None:
        pass

    def speak(self, text: str) -> float | None:
        self.speak_calls.append(text)
        self._release.clear()
        self.started.set()
        if self.blocking:
            self._release.wait(timeout=5)
        return 0.01

    def stop(self) -> None:
        self.stop_calls += 1
        self._release.set()

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        self.speak_calls.append(text)
        return np.zeros(1, dtype="float32"), 16000


class FakeFeedback:
    """speech_to_speech.feedback stand-in -- records cue names instead of
    actually shelling out to espeak-ng/say, so running the test suite
    doesn't audibly speak "Start"/"Ende" on every on_toggle() call."""

    def __init__(self):
        self.cue_calls: list[str] = []

    def play_cue(self, name: str) -> None:
        self.cue_calls.append(name)
