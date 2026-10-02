"""Test doubles for App's external collaborators -- the agent backend
(agent_backend.AgentConversation: send/cancel/close) and the STT/TTS daemon
clients (stt_client.py/tts_client.py) -- so the state-model tests in
test_core_dialog_loop.py can drive App without a real LLM, microphone, or
TTS daemon.

See docs/specs/core-dialog-loop.md for the model these are testing against.
"""

from __future__ import annotations

import threading

import numpy as np


class FakeAgentConversation:
    """Stands in for ClaudeCodeConversation/PiAgentConversation behind the
    AgentConversation protocol.

    By default `send()` returns `reply` immediately. With `gate=True`,
    every call to `send()` blocks until released -- either by the test
    calling `release()`, or by `cancel()` (a real backend's cancel() is a
    best-effort interrupt of whatever's in flight, so the fake's cancel()
    unblocks the currently pending send() the same way). A *fresh* release
    gate is created per call, so a second turn on the same fake blocks
    again even after an earlier cancel() -- matching that App reuses one
    backend instance across turns in a session.

    This is what makes "catch App in Processing, then do X" deterministic
    in tests instead of relying on sleeps/timing.
    """

    def __init__(self, reply: str = "ok", *, gate: bool = False, raises: Exception | None = None):
        self.reply = reply
        self.raises = raises
        self.gate = gate
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
        self.started.set()
        if self.gate:
            release.wait(timeout=5)
        if self.raises is not None:
            raise self.raises
        return self.reply

    def cancel(self) -> None:
        self.cancel_calls += 1
        self._release.set()

    def release(self) -> None:
        self._release.set()

    def close(self) -> None:
        self.close_calls += 1


class FakeSTT:
    """speech_to_speech.stt_client stand-in."""

    def __init__(self, transcript: str = "hello"):
        self.transcript = transcript
        self.start_calls = 0
        self.stop_calls = 0

    def ensure_available(self) -> None:
        pass

    def start_recording(self) -> None:
        self.start_calls += 1

    def stop_recording(self) -> str:
        self.stop_calls += 1
        return self.transcript

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
