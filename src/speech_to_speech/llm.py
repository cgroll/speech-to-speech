"""LLM backend: Claude Code Agent SDK with full tool capabilities.

Runs a persistent ClaudeSDKClient in a background asyncio event loop so the
synchronous send() interface that App expects is preserved unchanged.
"""

import asyncio
import logging
import threading

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from speech_to_speech.agent_backend import SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class ClaudeCodeConversation:
    def __init__(self, resume: str | None = None) -> None:
        # `resume` is a past session_id (see sessions.py / docs/backlog.md,
        # "Frühere Sessions wieder aufnehmen können") -- the SDK loads that
        # session's history from its own on-disk JSONL transcript and
        # continues it, so no conversation state needs to be reconstructed
        # here beyond what App.resume_session() rebuilds for cockpit display.
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._start(resume), self._loop).result(timeout=30)

    async def _start(self, resume: str | None) -> None:
        options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            system_prompt=SYSTEM_PROMPT,
            resume=resume,
        )
        self._mgr = ClaudeSDKClient(options=options)
        self._client = await self._mgr.__aenter__()

    def send(self, text: str) -> str:
        logger.info("Sending to Claude Code: %s", text)
        return asyncio.run_coroutine_threadsafe(
            self._query(text), self._loop
        ).result(timeout=300)

    def cancel(self) -> None:
        """Best-effort interrupt of an in-flight query (e.g. on barge-in).

        Fire-and-forget: does not block on the result, so it never delays the
        caller -- typically a button-press thread that needs to move on to
        starting a new recording right away. Safe to call even if nothing is
        in flight (e.g. barge-in during "speaking", after the query already
        finished); the SDK just has nothing to interrupt.
        """
        future = asyncio.run_coroutine_threadsafe(self._client.interrupt(), self._loop)
        future.add_done_callback(_log_if_failed)

    def close(self) -> None:
        """Tears down the SDK client and stops this instance's event-loop
        thread. Used by the cockpit's "new session" reset, which replaces
        `App._llm` with a freshly-constructed `ClaudeCodeConversation` and
        closes the old one afterwards -- best-effort, since by the time this
        runs nothing should still be calling into the old instance."""

        async def _stop() -> None:
            await self._mgr.__aexit__(None, None, None)

        try:
            asyncio.run_coroutine_threadsafe(_stop(), self._loop).result(timeout=10)
        except Exception:
            logger.warning("Error closing Claude client during reset", exc_info=True)
        self._loop.call_soon_threadsafe(self._loop.stop)

    async def _query(self, text: str) -> str:
        await self._client.query(text)
        parts: list[str] = []
        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(block.text, end="", flush=True)
                        parts.append(block.text)
            elif isinstance(msg, ResultMessage) and msg.result and not parts:
                print(msg.result, flush=True)
                return msg.result
        print()
        reply = "".join(parts).strip()
        logger.info("Claude Code reply: %s", reply[:200])
        return reply


def _log_if_failed(future: asyncio.Future) -> None:
    exc = future.exception()
    if exc is not None:
        logger.warning("Interrupt failed: %s", exc)
