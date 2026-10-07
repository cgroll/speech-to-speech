"""Tests for Recorder's stale-PortAudio-device-list recovery.

See audio.py's _open_stream() docstring comment: the STT daemon is
long-lived and initializes PortAudio once at startup, so a Jabra
disconnect/reconnect later can leave it with a stale device list that
fails to open with an opaque "Internal PortAudio error". Recorder.start()
is expected to recover from exactly one such failure by reinitializing
PortAudio and retrying, without the caller (daemon.py) ever seeing it.
"""

from __future__ import annotations

import sounddevice as sd

from speech_to_speech.dictate.audio import Recorder


class _FakeStream:
    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True


def test_start_recovers_from_one_stale_device_list_error(monkeypatch):
    attempts = []
    reinit_calls = []
    stream = _FakeStream()

    def fake_new_stream(self):
        attempts.append(1)
        if len(attempts) == 1:
            raise sd.PortAudioError("Error opening InputStream: Internal PortAudio error [PaErrorCode -9986]")
        return stream

    monkeypatch.setattr(Recorder, "_new_stream", fake_new_stream)
    monkeypatch.setattr(sd, "_terminate", lambda: reinit_calls.append("terminate"))
    monkeypatch.setattr(sd, "_initialize", lambda: reinit_calls.append("initialize"))

    recorder = Recorder()
    recorder.start()

    assert len(attempts) == 2
    assert reinit_calls == ["terminate", "initialize"]
    assert recorder._stream is stream
    assert stream.started


def test_start_gives_up_after_a_second_failure(monkeypatch):
    def always_fails(self):
        raise sd.PortAudioError("still broken")

    monkeypatch.setattr(Recorder, "_new_stream", always_fails)
    monkeypatch.setattr(sd, "_terminate", lambda: None)
    monkeypatch.setattr(sd, "_initialize", lambda: None)

    recorder = Recorder()
    try:
        recorder.start()
        assert False, "expected PortAudioError to propagate"
    except sd.PortAudioError:
        pass

    assert recorder._stream is None
