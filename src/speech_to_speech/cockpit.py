"""Web-Cockpit: a third trigger interface next to the Jabra button and the
local hotkey socket (see docs/architecture-proposal.md, "Web-Cockpit als
drittes Steuer-Interface"). Gradio was chosen over Dash because `gr.Chatbot`
gives chat-bubble rendering for free.

Runs in its own background thread in the same process as `app.py` (started
from `App.run()`, analogous to the Jabra listener thread). The single button
below calls straight into `App.on_toggle()` -- the exact same method the
Jabra button and local hotkey call -- so `App`'s existing state lock already
covers clicks made here too; no new network protocol between this UI and the
app logic, just a normal Python call.

Live state/history/stats are refreshed via Gradio's Timer component polling
every `COCKPIT_POLL_SECONDS` -- real websocket push isn't needed since the
actual audio still plays over the local speakers, not through the browser.
"""

import logging
import subprocess
import warnings

import gradio as gr
import numpy as np
from starlette.exceptions import StarletteDeprecationWarning

from speech_to_speech import sessions
from speech_to_speech.agent_backend import AGENT_LABELS
from speech_to_speech.config import COCKPIT_HOST, COCKPIT_PORT, COCKPIT_POLL_SECONDS
from speech_to_speech.dictate.audio import SAMPLE_RATE as STT_SAMPLE_RATE

logger = logging.getLogger(__name__)

# Gradio's own request-polling (queue keepalive HEAD/GET calls) logs at INFO
# via httpx, and gradio/routes.py still references the old Starlette
# constant name, triggering a StarletteDeprecationWarning on every request.
# Note: StarletteDeprecationWarning subclasses UserWarning, not
# DeprecationWarning, so it must be filtered by its own class. Both warnings
# are internal to Gradio, not actionable here -- silence them.
logging.getLogger("httpx").setLevel(logging.WARNING)
warnings.filterwarnings(
    "ignore",
    category=StarletteDeprecationWarning,
    module="gradio.routes",
)


def _to_stt_pcm(sample_rate: int, data: np.ndarray) -> np.ndarray:
    """Converts Gradio's raw microphone capture (native sample rate, int or
    float samples, mono or stereo) into the 16 kHz mono float32 PCM the STT
    daemon's `transcribe` command expects -- same target format as the
    Telegram bot's Ogg/Opus decode (telegram_bot/daemon.py's
    _decode_ogg_to_pcm), just a different source format on the way in."""
    if data.dtype.kind in ("i", "u"):
        data = data.astype(np.float32) / np.iinfo(data.dtype).max
    else:
        data = data.astype(np.float32)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sample_rate == STT_SAMPLE_RATE:
        return data
    proc = subprocess.run(
        [
            "ffmpeg",
            "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
            "-f", "f32le", "-ar", str(STT_SAMPLE_RATE), "-ac", "1", "pipe:1",
        ],
        input=data.tobytes(),
        capture_output=True,
        check=True,
    )
    return np.frombuffer(proc.stdout, dtype=np.float32)


def _snapshot(app) -> tuple[str, list[dict[str, str]], str]:
    # The active agent isn't in this tuple even though App tracks it -- it's
    # surfaced via get_stats_text()'s "Agent: ..." prefix instead of its own
    # output. This snapshot feeds the 0.3s poll timer (COCKPIT_POLL_SECONDS)
    # too; wiring the agent_picker Radio into it would snap that control
    # back to the *current* agent several times a second, making it
    # impossible to actually pick a different one before clicking "Neue
    # Session". agent_picker is instead only ever written to explicitly, by
    # _resume_session() below.
    return app.get_state(), app.get_history(), app.get_stats_text()


def build(app) -> gr.Blocks:
    with gr.Blocks(title="Speech-to-Speech Cockpit") as demo:
        gr.Markdown("# Speech-to-Speech Cockpit")

        state_box = gr.Textbox(label="Status", interactive=False)
        # Gradio 6's Chatbot always takes {"role", "content"} message dicts
        # now (the old type="messages" kwarg was removed as a no-longer-
        # needed choice) -- matches App.get_history()'s format directly.
        chatbot = gr.Chatbot(label="Verlauf", height=480)
        stats_box = gr.Textbox(label="Kennzahlen", interactive=False)

        # Agent-Wahl für die *nächste* neue Session (docs/backlog.md,
        # "Mehrere Agent-Backends", Entscheidung 3): wirkt nur beim Klick auf
        # "Neue Session", nicht mitten in einer laufenden -- kein
        # Backend-Wechsel innerhalb einer Session.
        agent_picker = gr.Radio(
            label="Agent für neue Session",
            choices=[(label, agent_id) for agent_id, label in AGENT_LABELS.items()],
            value=app.get_agent_name(),
        )

        with gr.Row():
            toggle_btn = gr.Button("Aufnehmen / Stoppen / Unterbrechen", variant="primary")
            reset_btn = gr.Button("Neue Session")

        # Browser-Mikrofon-Ein-/Ausgabe -- erster, bewusst einfacher
        # (nicht-gestreamter) Test-Roundtrip für den mobilen Zugriff
        # (docs/architecture-proposal.md, "Offene Frage: mobiler Zugriff
        # (Handy)"): unabhängig vom Jabra-/Hotkey-Pfad oben, kein Barge-in.
        # Aufnahme endet automatisch (stop_recording-Event, wie beim
        # Loslassen einer Sprachnachrichtentaste), dann läuft der ganze
        # Turn synchron durch (App.voice_turn) und die Antwort landet als
        # Audio-Clip zum Abspielen rechts daneben.
        with gr.Row():
            voice_input = gr.Audio(
                label="Sprachnachricht aufnehmen (Test, für mobilen Zugriff)",
                sources=["microphone"],
                type="numpy",
            )
            voice_output = gr.Audio(
                label="Antwort",
                type="numpy",
                autoplay=True,
            )

        # Session-Verlauf/-Wiederaufnahme (docs/backlog.md, "Frühere
        # Sessions wieder aufnehmen können" / "Mehrere Agent-Backends").
        # Persistierung übernimmt bereits der jeweilige Backend selbst
        # (sessions.py liest beide Formate und tagged sie); hier nur Anzeige
        # + Auswahl über eine gemeinsame Liste.
        with gr.Accordion("Frühere Sessions", open=False):
            session_picker = gr.Radio(
                label="Session auswählen",
                choices=sessions.session_choices(),
                value=None,
            )
            with gr.Row():
                refresh_sessions_btn = gr.Button("Liste aktualisieren")
                resume_session_btn = gr.Button("Ausgewählte Session fortsetzen", variant="secondary")

        # Alternative to the mic: type or paste text directly, e.g. when
        # dictating would be slower or more awkward than copy-pasting.
        # Shares App.on_toggle()'s pipeline via App.submit_text(), just
        # entering it at "thinking" instead of "recording".
        with gr.Row():
            text_input = gr.Textbox(
                label="Text eingeben (statt Sprache)",
                placeholder="Text hier einfügen und Enter drücken zum Senden …",
                scale=4,
            )
            send_btn = gr.Button("Senden", scale=1)

        outputs = [state_box, chatbot, stats_box]
        text_outputs = outputs + [text_input]
        # Only reset/resume explicitly write agent_picker (see _snapshot's
        # comment on why the fast poll below must not touch it).
        agent_outputs = outputs + [agent_picker]
        voice_outputs = outputs + [voice_output]

        def _toggle():
            app.on_toggle()
            return _snapshot(app)

        def _voice_submit(audio: tuple[int, np.ndarray] | None):
            if audio is None:
                return (*_snapshot(app), None)
            sample_rate, data = audio
            pcm = _to_stt_pcm(sample_rate, data)
            result = app.voice_turn(pcm)
            if result is None:
                return (*_snapshot(app), None)
            reply_audio, reply_sample_rate = result
            return (*_snapshot(app), (reply_sample_rate, reply_audio))

        def _reset(agent: str):
            app.reset(agent=agent)
            return (*_snapshot(app), app.get_agent_name())

        def _submit_text(text: str):
            app.submit_text(text)
            return (*_snapshot(app), "")

        def _refresh_sessions():
            return gr.update(choices=sessions.session_choices())

        def _resume_session(choice: str | None):
            app.resume_session(choice)
            return (*_snapshot(app), app.get_agent_name())

        toggle_btn.click(_toggle, outputs=outputs)
        voice_input.stop_recording(_voice_submit, inputs=voice_input, outputs=voice_outputs)
        reset_btn.click(_reset, inputs=agent_picker, outputs=agent_outputs)
        text_input.submit(_submit_text, inputs=text_input, outputs=text_outputs)
        send_btn.click(_submit_text, inputs=text_input, outputs=text_outputs)
        refresh_sessions_btn.click(_refresh_sessions, outputs=session_picker)
        # Refreshing after resume/reset keeps the list's "last modified"
        # ordering and the newly-started/continued session's own entry
        # current, without polling the filesystem on every timer tick.
        resume_session_btn.click(_resume_session, inputs=session_picker, outputs=agent_outputs).then(
            _refresh_sessions, outputs=session_picker
        )
        reset_btn.click(_refresh_sessions, outputs=session_picker)

        timer = gr.Timer(COCKPIT_POLL_SECONDS)
        timer.tick(lambda: _snapshot(app), outputs=outputs)
        demo.load(lambda: _snapshot(app), outputs=outputs)
        # Refreshes the session list per browser connection (not on the fast
        # state-poll timer -- listing sessions means a filesystem scan,
        # unlike the in-memory snapshot above) so a tab left open overnight
        # still shows sessions started elsewhere since the server booted.
        demo.load(_refresh_sessions, outputs=session_picker)

    return demo


def run(app) -> None:
    """Blocks the calling thread for as long as the cockpit server runs --
    call this from a dedicated background thread, same pattern as
    input_button.listen_for_toggle()."""
    demo = build(app)
    logger.info("Web cockpit listening on http://%s:%d", COCKPIT_HOST, COCKPIT_PORT)
    demo.queue().launch(
        server_name=COCKPIT_HOST,
        server_port=COCKPIT_PORT,
        prevent_thread_lock=False,
        quiet=True,
        show_error=True,
        inbrowser=False,
    )
