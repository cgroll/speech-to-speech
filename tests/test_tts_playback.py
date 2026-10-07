"""Tests for the TTS daemon's stale-PortAudio-device-list recovery.

Mirrors test_dictate_audio.py's coverage of the same pattern on the input
side: _open_output_stream() is expected to recover from exactly one
PortAudioError (e.g. after a Bluetooth reconnect leaves the long-lived TTS
daemon's device list stale) by reinitializing PortAudio and retrying,
instead of requiring a manual daemon restart.
"""

from __future__ import annotations

import sounddevice as sd

from speech_to_speech.tts_daemon import playback


def test_open_output_stream_recovers_from_one_stale_device_list_error(monkeypatch):
    attempts = []
    reinit_calls = []
    sentinel = object()

    def fake_output_stream(*, samplerate, channels, dtype, device):
        attempts.append(1)
        if len(attempts) == 1:
            raise sd.PortAudioError("Error opening OutputStream: Internal PortAudio error [PaErrorCode -9986]")
        return sentinel

    monkeypatch.setattr(sd, "OutputStream", fake_output_stream)
    monkeypatch.setattr(sd, "_terminate", lambda: reinit_calls.append("terminate"))
    monkeypatch.setattr(sd, "_initialize", lambda: reinit_calls.append("initialize"))
    # _open_output_stream's retry re-resolves the device via _output_device(),
    # which on macOS re-queries PortAudio -- stub that out so the test is
    # deterministic regardless of whether a real Jabra happens to be plugged
    # into the machine running it.
    monkeypatch.setattr(sd, "query_devices", lambda: [{"name": "Jabra Link 390", "max_output_channels": 2}])

    stream = playback._open_output_stream(24_000, device=0)

    assert len(attempts) == 2
    assert reinit_calls == ["terminate", "initialize"]
    assert stream is sentinel


def test_open_output_stream_gives_up_after_a_second_failure(monkeypatch):
    def always_fails(*, samplerate, channels, dtype, device):
        raise sd.PortAudioError("still broken")

    monkeypatch.setattr(sd, "OutputStream", always_fails)
    monkeypatch.setattr(sd, "_terminate", lambda: None)
    monkeypatch.setattr(sd, "_initialize", lambda: None)

    try:
        playback._open_output_stream(24_000, device=0)
        assert False, "expected PortAudioError to propagate"
    except sd.PortAudioError:
        pass
