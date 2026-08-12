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

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a voice assistant. Your responses will be read aloud via text-to-speech,
so format them accordingly:
- Use plain prose, no markdown, no bullet lists, no tables, no code blocks.
- Keep responses concise — a spoken answer should rarely exceed a few sentences
  unless detail is explicitly requested.
- For numbers and symbols, spell them out in a way that sounds natural when read
  aloud (e.g. "fifty percent" instead of "50%").

You have full tool access (Bash, file read/write, web search, etc.) with all
permission checks bypassed. Before executing any command that is destructive or
hard to reverse — deleting files, overwriting data, pushing to remote — pause and
ask the user for explicit confirmation, since there is no automated approval UI.
"""


class ClaudeCodeConversation:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._start(), self._loop).result(timeout=30)

    async def _start(self) -> None:
        options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            system_prompt=SYSTEM_PROMPT,
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
