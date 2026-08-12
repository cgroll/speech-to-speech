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
import warnings

import gradio as gr
from starlette.exceptions import StarletteDeprecationWarning

from speech_to_speech import sessions
from speech_to_speech.agent_backend import AGENT_LABELS
from speech_to_speech.config import COCKPIT_HOST, COCKPIT_PORT, COCKPIT_POLL_SECONDS

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

        def _toggle():
            app.on_toggle()
            return _snapshot(app)

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
