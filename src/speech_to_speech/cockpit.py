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
from pathlib import Path

import gradio as gr
import numpy as np
from starlette.exceptions import StarletteDeprecationWarning

from speech_to_speech import sessions
from speech_to_speech.agent_backend import AGENT_LABELS, resolve_workspace
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


def _list_subdirs(path: str) -> list[str]:
    """Sorted subfolder names directly inside `path`, for the workspace
    folder browser below. Swallows permission/missing-dir errors (hitting
    e.g. a restricted system folder shouldn't crash the cockpit) by just
    returning an empty list, which renders as an empty Radio -- "Eine Ebene
    höher" still works from there."""
    try:
        entries = sorted(Path(path).iterdir())
    except OSError:
        return []
    names = []
    for entry in entries:
        try:
            if entry.is_dir():
                names.append(entry.name)
        except OSError:
            continue
    return names


def build(app) -> gr.Blocks:
    with gr.Blocks(title="Speech-to-Speech Cockpit") as demo:
        gr.Markdown("# Speech-to-Speech Cockpit")

        # --- 1. Status & Voice Toggle ---
        with gr.Row():
            state_box = gr.Textbox(label="Status", interactive=False, scale=3)
            # Text-only-Modus (siehe docs/backlog.md): unterdrückt die
            # tatsächliche TTS-Synthese/Wiedergabe (App._speak()/voice_turn()),
            # lässt aber Status/Verlauf/Statistiken unangetastet -- Antworten
            # erscheinen weiter sofort im Chat-Verlauf, nur eben lautlos. Wirkt
            # sofort (kein Session-Neustart nötig), da es kein Teil des
            # Backend-Zustands ist, der mit der Session wechselt.
            voice_mute_box = gr.Checkbox(
                label="Audio-Ausgabe stumm (nur Text)",
                value=app.get_voice_muted(),
                scale=1,
            )

        # --- 2. Ein- und Ausgaben ---
        with gr.Group():
            # Gradio 6's Chatbot always takes {"role", "content"} message dicts
            # now (the old type="messages" kwarg was removed as a no-longer-
            # needed choice) -- matches App.get_history()'s format directly.
            chatbot = gr.Chatbot(label="Verlauf", height=480)
            stats_box = gr.Textbox(label="Kennzahlen", interactive=False)

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

            with gr.Row():
                toggle_btn = gr.Button("Aufnehmen / Stoppen / Unterbrechen", variant="primary")
                # Separate from toggle_btn: pressing the toggle while "thinking"/
                # "speaking" is a barge-in and jumps straight into a *new*
                # recording (on_toggle()'s design -- see App docstring). This
                # button is for the plain "stop talking, I'm not about to say
                # anything" case instead: App.stop() cancels/interrupts and goes
                # back to idle without starting to listen.
                stop_btn = gr.Button("Nur stoppen (ohne neue Aufnahme)")

            # Browser-Mikrofon-Ein-/Ausgabe -- erster, bewusst einfacher
            # (nicht-gestreamter) Test-Roundtrip für den mobilen Zugriff
            # (docs/architecture-proposal.md, "Offene Frage: mobiler Zugriff
            # (Handy)"): unabhängig vom Jabra-/Hotkey-Pfad oben, kein Barge-in.
            # Aufnahme endet automatisch (stop_recording-Event, wie beim
            # Loslassen einer Sprachnachrichtentaste), dann läuft der ganze
            # Turn synchron durch (App.voice_turn) and die Antwort landet als
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

        # --- 3. Agent Configuration (Agent, Session, Workspace) ---
        with gr.Accordion("Agenten-Konfiguration (Agent, Session, Workspace)", open=False):
            # Agent-Wahl für die *nächste* neue Session (docs/backlog.md,
            # "Mehrere Agent-Backends", Entscheidung 3): wirkt nur beim Klick auf
            # "Neue Session", nicht mitten in einer laufenden -- kein
            # Backend-Wechsel innerhalb einer Session.
            with gr.Row():
                agent_picker = gr.Radio(
                    label="Agent für neue Session",
                    choices=[(label, agent_id) for agent_id, label in AGENT_LABELS.items()],
                    value=app.get_agent_name(),
                    scale=3,
                )
                reset_btn = gr.Button("Neue Session starten", scale=1)

            # Session-Verlauf/-Wiederaufnahme (docs/backlog.md, "Frühere
            # Sessions wieder aufnehmen können" / "Mehrere Agent-Backends").
            # Persistierung übernimmt bereits der jeweilige Backend selbst
            # (sessions.py liest beide Formate und tagged sie); hier nur Anzeige
            # + Auswahl über eine gemeinsame Liste.
            with gr.Group():
                gr.Markdown("### Frühere Sessions")
                session_picker = gr.Radio(
                    label="Session auswählen",
                    choices=sessions.session_choices(),
                    value=None,
                )
                with gr.Row():
                    refresh_sessions_btn = gr.Button("Liste aktualisieren")
                    resume_session_btn = gr.Button("Ausgewählte Session fortsetzen", variant="secondary")

            # Workspace-Wahl für die *nächste* neue Session (docs/backlog.md,
            # "Mehrere Agent-Backends", Punkt 2), gleiches Muster wie
            # agent_picker oben: wirkt nur beim Klick auf "Neue Session", kein
            # Wechsel mitten in einer laufenden. workspace_box zeigt/erlaubt den
            # Pfad direkt als Text.
            with gr.Group():
                gr.Markdown("### Workspace")
                workspace_box = gr.Textbox(
                    label="Workspace-Pfad",
                    value=app.get_workspace(),
                )
                browse_dir_state = gr.State(str(Path.home()))
                browse_label = gr.Markdown(f"📁 {Path.home()}")
                browse_up_btn = gr.Button("⬆️ Eine Ebene höher", size="sm")
                browse_list = gr.Radio(
                    label="Unterordner (anklicken: auswählen & reingehen)",
                    choices=_list_subdirs(str(Path.home())),
                    value=None,
                )

        # --- 4. Infrastruktur-Steuerung ---
        # Manueller Neustart der Hintergrund-Daemons (docs/backlog.md,
        # "Daemon-Neustart aus der App/dem Cockpit heraus") -- für den Fall,
        # dass ein Daemon zwar noch antwortet, sich aber falsch/festgefahren
        # verhält, ohne dass der TTS-Watchdog anschlägt. Läuft über
        # App.restart_stt_daemon()/restart_tts_daemon(), die eine laufende
        # Aufnahme/Wiedergabe vorher sauber abbrechen.
        with gr.Accordion("Infrastruktur-Steuerung (Daemons)", open=False):
            with gr.Row():
                restart_stt_btn = gr.Button("STT-Daemon neu starten", variant="secondary")
                restart_tts_btn = gr.Button("TTS-Daemon neu starten", variant="secondary")

        outputs = [state_box, chatbot, stats_box]
        text_outputs = outputs + [text_input]
        # Only reset/resume explicitly write agent_picker/workspace_box (see
        # _snapshot's comment on why the fast poll below must not touch
        # them -- workspace_box doubles as a free-text field the user might
        # be mid-typing in, same reasoning as agent_picker's Radio).
        session_start_outputs = outputs + [agent_picker, workspace_box]
        voice_outputs = outputs + [voice_output]

        def _toggle():
            app.on_toggle()
            return _snapshot(app)

        def _stop():
            app.stop()
            return _snapshot(app)

        def _set_voice_muted(muted: bool):
            app.set_voice_muted(muted)

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

        def _browse_into(current_dir: str, selected: str | None):
            if not selected:
                return gr.update(), gr.update(), gr.update(), current_dir
            new_dir = str(Path(current_dir) / selected)
            return (
                new_dir,
                f"📁 {new_dir}",
                gr.update(choices=_list_subdirs(new_dir), value=None),
                new_dir,
            )

        def _browse_up(current_dir: str):
            new_dir = str(Path(current_dir).parent)
            return (
                new_dir,
                f"📁 {new_dir}",
                gr.update(choices=_list_subdirs(new_dir), value=None),
                new_dir,
            )

        def _reset(agent: str, workspace: str):
            try:
                workspace = resolve_workspace(workspace)
            except ValueError as exc:
                gr.Warning(f"Ungültiger Workspace, unverändert gelassen: {exc}")
                workspace = app.get_workspace()
            app.reset(agent=agent, workspace=workspace)
            return (*_snapshot(app), app.get_agent_name(), app.get_workspace())

        def _submit_text(text: str):
            app.submit_text(text)
            return (*_snapshot(app), "")

        def _refresh_sessions():
            return gr.update(choices=sessions.session_choices())

        def _restart_stt():
            try:
                app.restart_stt_daemon()
                gr.Info("STT-Daemon neu gestartet.")
            except Exception as exc:  # noqa: BLE001 - surface to the UI, keep the cockpit alive
                logger.exception("STT daemon restart failed")
                gr.Warning(f"STT-Daemon-Neustart fehlgeschlagen: {exc}")
            return _snapshot(app)

        def _restart_tts():
            try:
                app.restart_tts_daemon()
                gr.Info("TTS-Daemon neu gestartet.")
            except Exception as exc:  # noqa: BLE001 - surface to the UI, keep the cockpit alive
                logger.exception("TTS daemon restart failed")
                gr.Warning(f"TTS-Daemon-Neustart fehlgeschlagen: {exc}")
            return _snapshot(app)

        def _resume_session(choice: str | None):
            app.resume_session(choice)
            return (*_snapshot(app), app.get_agent_name(), app.get_workspace())

        toggle_btn.click(_toggle, outputs=outputs)
        stop_btn.click(_stop, outputs=outputs)
        voice_mute_box.change(_set_voice_muted, inputs=voice_mute_box)
        restart_stt_btn.click(_restart_stt, outputs=outputs)
        restart_tts_btn.click(_restart_tts, outputs=outputs)
        voice_input.stop_recording(_voice_submit, inputs=voice_input, outputs=voice_outputs)
        browse_list.change(
            _browse_into,
            inputs=[browse_dir_state, browse_list],
            outputs=[workspace_box, browse_label, browse_list, browse_dir_state],
        )
        browse_up_btn.click(
            _browse_up,
            inputs=browse_dir_state,
            outputs=[workspace_box, browse_label, browse_list, browse_dir_state],
        )
        reset_btn.click(_reset, inputs=[agent_picker, workspace_box], outputs=session_start_outputs)
        text_input.submit(_submit_text, inputs=text_input, outputs=text_outputs)
        send_btn.click(_submit_text, inputs=text_input, outputs=text_outputs)
        refresh_sessions_btn.click(_refresh_sessions, outputs=session_picker)
        # Refreshing after resume/reset keeps the list's "last modified"
        # ordering and the newly-started/continued session's own entry
        # current, without polling the filesystem on every timer tick.
        resume_session_btn.click(
            _resume_session, inputs=session_picker, outputs=session_start_outputs
        ).then(_refresh_sessions, outputs=session_picker)
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
