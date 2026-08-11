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


class ClaudeCodeConversation:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._start(), self._loop).result(timeout=30)

    async def _start(self) -> None:
        options = ClaudeAgentOptions(permission_mode="bypassPermissions")
        self._mgr = ClaudeSDKClient(options=options)
        self._client = await self._mgr.__aenter__()

    def send(self, text: str) -> str:
        logger.info("Sending to Claude Code: %s", text)
        return asyncio.run_coroutine_threadsafe(
            self._query(text), self._loop
        ).result(timeout=300)

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
