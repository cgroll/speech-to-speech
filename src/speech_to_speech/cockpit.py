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

        with gr.Row():
            toggle_btn = gr.Button("Aufnehmen / Stoppen / Unterbrechen", variant="primary")
            reset_btn = gr.Button("Neue Session")

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

        def _toggle():
            app.on_toggle()
            return _snapshot(app)

        def _reset():
            app.reset()
            return _snapshot(app)

        def _submit_text(text: str):
            app.submit_text(text)
            state, history, stats = _snapshot(app)
            return state, history, stats, ""

        toggle_btn.click(_toggle, outputs=outputs)
        reset_btn.click(_reset, outputs=outputs)
        text_input.submit(_submit_text, inputs=text_input, outputs=text_outputs)
        send_btn.click(_submit_text, inputs=text_input, outputs=text_outputs)

        timer = gr.Timer(COCKPIT_POLL_SECONDS)
        timer.tick(lambda: _snapshot(app), outputs=outputs)
        demo.load(lambda: _snapshot(app), outputs=outputs)

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
