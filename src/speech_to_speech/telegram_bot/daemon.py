"""Telegram-bot daemon: long-polling bridge between Telegram and the same
agent backend the desktop app (app.py) talks to (docs/telegram-bot-proposal.md).

Text-only on the way out (no TTS here, by design) -- a message in gets a
message back:
- Text message -> straight to the agent, reply sent back as text.
- Voice message -> downloaded, decoded from Ogg/Opus to 16 kHz mono PCM via
  ffmpeg, transcribed by the shared STT daemon (stt_client.transcribe(),
  which bypasses the live-mic/VAD path entirely), the transcript sent back
  as its own message first (so recognition errors are visible separately
  from understanding errors), then fed into the agent exactly like a text
  message.

One AgentConversation per chat id, held in memory for the lifetime of this
process (analogous to App._llm, but one per chat instead of one for the
whole process) -- gives each chat continuity across messages, same as a
desktop session. Not persisted across daemon restarts; that's fine, the
underlying backend's own on-disk session transcript still exists and could
be resumed manually if that ever matters.

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
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from speech_to_speech import stt_client
from speech_to_speech.agent_backend import DEFAULT_AGENT, AgentConversation, create_conversation
from speech_to_speech.config import TELEGRAM_ALLOWED_CHAT_IDS, TELEGRAM_BOT_TOKEN
from speech_to_speech.dictate.audio import SAMPLE_RATE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


class _ChatSession:
    def __init__(self) -> None:
        self.conversation: AgentConversation = create_conversation(DEFAULT_AGENT)
        # Serializes turns within one chat (a chat client could in principle
        # fire off a second message before the first reply lands); separate
        # chats still run concurrently since each has its own session/lock.
        self.lock = threading.Lock()


_sessions: dict[int, _ChatSession] = {}
_sessions_lock = threading.Lock()


def _get_session(chat_id: int) -> _ChatSession:
    with _sessions_lock:
        session = _sessions.get(chat_id)
        if session is None:
            session = _ChatSession()
            _sessions[chat_id] = session
        return session


async def _ask_agent(chat_id: int, text: str) -> str:
    session = _get_session(chat_id)

    def _send() -> str:
        with session.lock:
            return session.conversation.send(text)

    return await asyncio.to_thread(_send)


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
    await update.message.reply_text(reply)


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
    await update.message.reply_text(reply)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set (see .env.example)", file=sys.stderr)
        sys.exit(1)
    if not TELEGRAM_ALLOWED_CHAT_IDS:
        print("Error: TELEGRAM_ALLOWED_CHAT_IDS not set (see .env.example)", file=sys.stderr)
        sys.exit(1)

    stt_client.ensure_available()

    allowed = filters.Chat(chat_id=[int(chat_id) for chat_id in TELEGRAM_ALLOWED_CHAT_IDS])
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, _handle_text))
    app.add_handler(MessageHandler(allowed & filters.VOICE, _handle_voice))

    logger.info(
        "Telegram bot ready (allowed chats: %s). Polling...", sorted(TELEGRAM_ALLOWED_CHAT_IDS)
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
