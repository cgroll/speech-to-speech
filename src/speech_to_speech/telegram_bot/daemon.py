"""Telegram-bot daemon: long-polling bridge between Telegram and the same
agent backend the desktop app (app.py) talks to (docs/telegram-bot-proposal.md).

A message in gets a message back:
- Text message -> straight to the agent, reply sent back as text or voice
  depending on the chat's output mode (see /voice below).
- Voice message -> downloaded, decoded from Ogg/Opus to 16 kHz mono PCM via
  ffmpeg, transcribed by the shared STT daemon (stt_client.transcribe(),
  which bypasses the live-mic/VAD path entirely), the transcript sent back
  as its own text message first (so recognition errors are visible
  separately from understanding errors), then fed into the agent exactly
  like a text message -- the reply again follows the chat's output mode.

Per-chat output mode (/voice) picks text or synthesized-voice replies,
independent of whether the *input* was typed or spoken -- reuses the shared
TTS daemon's synthesize() (tts_client.py, same one the Gradio cockpit's
audio round trip uses) rather than playing anything locally, then
re-encodes to Ogg/Opus via ffmpeg since that's what Telegram's voice-message
bubble requires (mirrors _decode_ogg_to_pcm's decode in the other
direction).

One AgentConversation per chat id, held in memory for the lifetime of this
process (analogous to App._llm, but one per chat instead of one for the
whole process) -- gives each chat continuity across messages, same as a
desktop session. Not persisted across daemon restarts; that's fine, the
underlying backend's own on-disk session transcript still exists and could
be resumed manually if that ever matters.

Slash commands mirror the cockpit's session controls (docs/backlog.md,
"Mehrere Agent-Backends"/"Frühere Sessions wieder aufnehmen können"), reusing
the same sessions.py this cockpit uses instead of building a second
listing/resume mechanism:
- /new [claude|pi] -- fresh session, optionally switching backend (no
  mid-session backend switch, same rule as the cockpit).
- /agent -- show which backend this chat is currently on.
- /sessions -- recent sessions (both backends, across all chats/callers --
  see sessions.py) as tappable inline buttons; tapping one resumes it.
- /voice [text|audio] -- show or switch this chat's reply mode; toggles
  between the two if called without an argument.

Long-polling, not a webhook -- no inbound port to open, the bot only makes
outbound connections to Telegram's servers (see the proposal doc for why).
"""

import asyncio
import logging
import subprocess
import sys
import threading

# Must run before the speech_to_speech.config import below -- config.py
# reads TELEGRAM_BOT_TOKEN/TELEGRAM_ALLOWED_CHAT_IDS from os.environ at
# *module import* time, so .env has to be loaded first or those come back
# empty (bit us once already: cli.py's main() calls load_dotenv() only
# after its own App import, which happens to work there since nothing it
# transitively imports needs an env-sourced value at import time -- not
# true here).
from dotenv import load_dotenv

load_dotenv()

import numpy as np
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from speech_to_speech import sessions, stt_client, tts_client
from speech_to_speech.agent_backend import AGENT_LABELS, DEFAULT_AGENT, AgentConversation, create_conversation
from speech_to_speech.config import TELEGRAM_ALLOWED_CHAT_IDS, TELEGRAM_BOT_TOKEN
from speech_to_speech.dictate.audio import SAMPLE_RATE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# /sessions lists at most this many -- sessions.MAX_LISTED_SESSIONS (20) is
# fine for a scrollable cockpit list, but that many inline-keyboard rows in
# a chat bubble is unwieldy on a phone.
_MAX_SESSION_BUTTONS = 10
# Inline-keyboard button text has no hard API limit but gets visually
# unwieldy long before that; session titles (first prompt/summary) can run
# long.
_MAX_BUTTON_LABEL = 60


_OUTPUT_MODES = ("text", "audio")


class _ChatSession:
    def __init__(self, agent: str, conversation: AgentConversation) -> None:
        self.agent = agent
        self.conversation = conversation
        # Per-chat, independent of whether the *input* was typed or spoken
        # -- a chat that sends voice messages might still want text replies
        # back (e.g. to read quietly), and vice versa. Plain str field, not
        # guarded by `lock` below: toggled by /voice only, a single
        # attribute assignment is already atomic under the GIL, and reading
        # a slightly-stale value while a toggle is in flight is harmless
        # (affects at most the reply to the message that raced it).
        self.output_mode = "text"
        # Serializes turns *and* resets within one chat (a chat client
        # could in principle fire off a second message -- or a /new --
        # before the first reply lands; separate chats still run
        # concurrently since each has its own session/lock). Reset holding
        # this lock is what keeps a /new or /sessions-resume from closing
        # the conversation object out from under a send() that's still
        # using it.
        self.lock = threading.Lock()


_sessions: dict[int, _ChatSession] = {}
_sessions_lock = threading.Lock()


def _get_session(chat_id: int) -> _ChatSession:
    with _sessions_lock:
        session = _sessions.get(chat_id)
        if session is None:
            session = _ChatSession(DEFAULT_AGENT, create_conversation(DEFAULT_AGENT))
            _sessions[chat_id] = session
        return session


async def _ask_agent(chat_id: int, text: str) -> str:
    session = _get_session(chat_id)

    def _send() -> str:
        with session.lock:
            return session.conversation.send(text)

    return await asyncio.to_thread(_send)


async def _reset_session(chat_id: int, agent: str, resume: str | None = None) -> None:
    """Shared implementation of /new and the /sessions resume button: builds
    the new backend client *before* taking the lock (construction can take
    seconds -- no reason to block a concurrent send() on this chat for that
    long), then swaps it in and closes the old one under the lock so a
    send() that's mid-flight on the old conversation can't have it closed
    out from under it."""
    session = _get_session(chat_id)

    def _do() -> None:
        new_conversation = create_conversation(agent, resume=resume)
        with session.lock:
            old_conversation = session.conversation
            session.conversation = new_conversation
            session.agent = agent
        old_conversation.close()

    await asyncio.to_thread(_do)


def _decode_ogg_to_pcm(ogg_bytes: bytes) -> np.ndarray:
    """Ogg/Opus (Telegram's voice-message format) -> 16 kHz mono float32 PCM
    via ffmpeg, matching what the STT daemon's `transcribe` command expects
    (dictate/audio.py's SAMPLE_RATE, incidentally the same 16 kHz Telegram
    already uses -- no resampling surprise either way)."""
    proc = subprocess.run(
        ["ffmpeg", "-i", "pipe:0", "-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "1", "pipe:1"],
        input=ogg_bytes,
        capture_output=True,
        check=True,
    )
    return np.frombuffer(proc.stdout, dtype=np.float32)


def _encode_pcm_to_ogg(audio: np.ndarray, sample_rate: int) -> bytes:
    """Reverse of _decode_ogg_to_pcm: the TTS daemon's synthesize() returns
    raw float32 PCM, but Telegram's voice-message bubble requires Ogg/Opus
    -- re-encode via ffmpeg before handing it to reply_voice()."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
            "-c:a", "libopus", "-f", "ogg", "pipe:1",
        ],
        input=audio.tobytes(),
        capture_output=True,
        check=True,
    )
    return proc.stdout


async def _send_reply(update: Update, chat_id: int, text: str) -> None:
    """Sends an agent reply back in the chat's current output mode
    (session.output_mode, toggled via /voice). Falls back to a text reply
    if synthesis fails, rather than swallowing the reply entirely -- a
    transient TTS-daemon hiccup shouldn't lose the answer."""
    if _get_session(chat_id).output_mode == "audio":
        try:
            audio, sample_rate = await asyncio.to_thread(tts_client.synthesize, text)
            if audio.size > 0:
                ogg_bytes = await asyncio.to_thread(_encode_pcm_to_ogg, audio, sample_rate)
                await update.message.reply_voice(voice=ogg_bytes)
                return
        except Exception:  # noqa: BLE001 - fall back to text, keep the daemon alive
            logger.exception("Voice synthesis failed, falling back to text")
    await update.message.reply_text(text)


async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    if not text:
        return
    logger.info("[%s] text: %s", chat_id, text)
    try:
        reply = await _ask_agent(chat_id, text)
    except Exception as exc:  # noqa: BLE001 - report to the chat, keep the daemon alive
        logger.exception("Agent call failed")
        await update.message.reply_text(f"Fehler: {exc}")
        return
    await _send_reply(update, chat_id, reply)


async def _handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    logger.info("[%s] voice message", chat_id)
    try:
        file = await context.bot.get_file(update.message.voice.file_id)
        ogg_bytes = bytes(await file.download_as_bytearray())
        audio = await asyncio.to_thread(_decode_ogg_to_pcm, ogg_bytes)
        text = (await asyncio.to_thread(stt_client.transcribe, audio)).strip()
    except Exception as exc:  # noqa: BLE001 - report to the chat, keep the daemon alive
        logger.exception("Voice transcription failed")
        await update.message.reply_text(f"Fehler bei der Spracherkennung: {exc}")
        return

    if not text:
        await update.message.reply_text("(keine Sprache erkannt)")
        return

    # Echo the transcript back as its own message first -- lets recognition
    # errors be told apart from understanding errors (see the proposal doc);
    # deliberately no audio echo, the sent voice message is already visible
    # in the chat history.
    await update.message.reply_text(f"Transkript: {text}")

    try:
        reply = await _ask_agent(chat_id, text)
    except Exception as exc:  # noqa: BLE001 - report to the chat, keep the daemon alive
        logger.exception("Agent call failed")
        await update.message.reply_text(f"Fehler: {exc}")
        return
    await _send_reply(update, chat_id, reply)


async def _cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    agent = _get_session(chat_id).agent
    if context.args:
        requested = context.args[0].lower()
        if requested not in AGENT_LABELS:
            await update.message.reply_text(
                f"Unbekannter Agent '{requested}'. Verfügbar: {', '.join(sorted(AGENT_LABELS))}"
            )
            return
        agent = requested

    try:
        await _reset_session(chat_id, agent)
    except Exception as exc:  # noqa: BLE001 - report to the chat, keep the daemon alive
        logger.exception("Reset failed")
        await update.message.reply_text(f"Fehler: {exc}")
        return
    await update.message.reply_text(f"Neue Session gestartet (Agent: {AGENT_LABELS.get(agent, agent)}).")


async def _cmd_agent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    agent = _get_session(chat_id).agent
    switches = ", ".join(f"/new {name}" for name in sorted(AGENT_LABELS) if name != agent)
    await update.message.reply_text(
        f"Aktueller Agent: {AGENT_LABELS.get(agent, agent)}\nWechseln (startet neue Session): {switches}"
    )


async def _cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = _get_session(chat_id)

    if context.args:
        requested = context.args[0].lower()
        if requested not in _OUTPUT_MODES:
            await update.message.reply_text(f"Unbekannter Modus '{requested}'. Verfügbar: text, audio")
            return
        session.output_mode = requested
    else:
        session.output_mode = "audio" if session.output_mode == "text" else "text"

    label = "Sprachnachricht" if session.output_mode == "audio" else "Text"
    await update.message.reply_text(f"Antwortmodus: {label}")


async def _cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        choices = await asyncio.to_thread(sessions.session_choices)
    except Exception as exc:  # noqa: BLE001 - report to the chat, keep the daemon alive
        logger.exception("Listing sessions failed")
        await update.message.reply_text(f"Fehler: {exc}")
        return

    if not choices:
        await update.message.reply_text("Keine früheren Sessions gefunden.")
        return

    keyboard = [
        [InlineKeyboardButton(label[:_MAX_BUTTON_LABEL], callback_data=value)]
        for label, value in choices[:_MAX_SESSION_BUTTONS]
    ]
    await update.message.reply_text(
        "Frühere Sessions -- zum Fortsetzen antippen:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def _handle_resume_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat.id
    if str(chat_id) not in TELEGRAM_ALLOWED_CHAT_IDS:
        # No allowlist filter on CallbackQueryHandler itself (unlike the
        # MessageHandlers below) -- Telegram routes a button tap straight
        # back to us regardless of who taps it, so this is the one place
        # the allowlist check has to happen inline instead.
        await query.answer()
        return

    await query.answer()
    choice = query.data or ""
    if ":" not in choice:
        return
    agent, session_id = choice.split(":", 1)
    if agent not in AGENT_LABELS:
        await query.edit_message_text("Ungültige Auswahl.")
        return

    try:
        await _reset_session(chat_id, agent, resume=session_id)
    except Exception as exc:  # noqa: BLE001 - report to the chat, keep the daemon alive
        logger.exception("Resume failed")
        await query.edit_message_text(f"Fehler: {exc}")
        return
    await query.edit_message_text(f"Session fortgesetzt ({AGENT_LABELS.get(agent, agent)}).")


async def _post_init(app: Application) -> None:
    """Registers the slash-command menu Telegram clients show next to the
    message box -- purely a discoverability nicety, the commands work via
    the handlers below regardless of whether this ran."""
    await app.bot.set_my_commands(
        [
            ("new", "Neue Session starten (optional: claude|pi)"),
            ("agent", "Aktuellen Agenten anzeigen"),
            ("sessions", "Frühere Sessions anzeigen/fortsetzen"),
            ("voice", "Antwortmodus umschalten (optional: text|audio)"),
        ]
    )


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set (see .env.example)", file=sys.stderr)
        sys.exit(1)
    if not TELEGRAM_ALLOWED_CHAT_IDS:
        print("Error: TELEGRAM_ALLOWED_CHAT_IDS not set (see .env.example)", file=sys.stderr)
        sys.exit(1)

    stt_client.ensure_available()
    tts_client.ensure_available()

    allowed = filters.Chat(chat_id=[int(chat_id) for chat_id in TELEGRAM_ALLOWED_CHAT_IDS])
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("new", _cmd_new, filters=allowed))
    app.add_handler(CommandHandler("agent", _cmd_agent, filters=allowed))
    app.add_handler(CommandHandler("sessions", _cmd_sessions, filters=allowed))
    app.add_handler(CommandHandler("voice", _cmd_voice, filters=allowed))
    app.add_handler(CallbackQueryHandler(_handle_resume_callback))
    app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, _handle_text))
    app.add_handler(MessageHandler(allowed & filters.VOICE, _handle_voice))

    logger.info(
        "Telegram bot ready (allowed chats: %s). Polling...", sorted(TELEGRAM_ALLOWED_CHAT_IDS)
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
