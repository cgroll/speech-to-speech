"""LLM backend: Claude Code Agent SDK with full tool capabilities.

Runs a persistent ClaudeSDKClient in a background asyncio event loop so the
synchronous send() interface that App expects is preserved unchanged.
"""

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
)

from speech_to_speech.agent_backend import LlmTimeoutError, system_prompt_for, workspace_instruction
from speech_to_speech.config import DEFAULT_WORKSPACE
from speech_to_speech.image_tool import SHOW_IMAGE_INSTRUCTION, make_show_image_tool

logger = logging.getLogger(__name__)

# No streamed message (assistant text, tool call/result, etc.) at all for
# this long -> treat the turn as stuck rather than just slow, per
# docs/backlog.md's "besser abfangen statt nur den Timeout erhöhen": a flat
# cap on the *whole* turn can't tell a genuinely stuck call apart from one
# that's still actively working through a long tool-heavy response, so this
# resets on every message instead of applying once to the total. Set back to
# 300s (2026-09-14) -- a real stall is rare enough that a tighter window
# wasn't worth the risk of cutting off a merely slow-but-active turn.
_ACTIVITY_TIMEOUT_S = 300

# Overall safety net in case activity keeps trickling in (a message every
# ~5min) without the turn ever actually finishing -- unlikely, but send()
# should still return eventually rather than block its caller forever.
_HARD_TIMEOUT_S = 20 * 60


class ClaudeCodeConversation:
    def __init__(
        self,
        resume: str | None = None,
        workspace: str | None = None,
        on_image: Callable[[str, str], None] | None = None,
        voice_output: bool = False,
    ) -> None:
        # `resume` is a past session_id (see sessions.py / docs/backlog.md,
        # "Frühere Sessions wieder aufnehmen können") -- the SDK loads that
        # session's history from its own on-disk JSONL transcript and
        # continues it, so no conversation state needs to be reconstructed
        # here beyond what App.resume_session() rebuilds for cockpit display.
        self._workspace = workspace or str(DEFAULT_WORKSPACE)
        # `on_image`, if given, wires up the show_image tool (image_tool.py)
        # -- the caller (App for the cockpit, telegram_bot/daemon.py per
        # chat) supplies where a shown image should actually go. None means
        # no delivery channel is available, so the tool isn't registered at
        # all rather than registered-but-broken.
        self._on_image = on_image
        # Whether this conversation's replies get spoken -- picks the voice vs.
        # text formatting half of the system prompt (set once in _start()).
        self._voice_output = voice_output
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._start(resume), self._loop).result(timeout=30)

    async def _start(self, resume: str | None) -> None:
        system_prompt = system_prompt_for(self._voice_output) + "\n\n" + workspace_instruction(self._workspace)
        mcp_servers = {}
        if self._on_image is not None:
            show_image_tool = make_show_image_tool(self._workspace, self._on_image)
            mcp_servers["images"] = create_sdk_mcp_server("images", tools=[show_image_tool])
            system_prompt += "\n\n" + SHOW_IMAGE_INSTRUCTION

        options = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            system_prompt=system_prompt,
            resume=resume,
            cwd=self._workspace,
            mcp_servers=mcp_servers,
            # Explicit per claude_agent_sdk's own docs ("the single place to
            # turn skills on"): without this, whether personal skills like
            # ~/.claude/skills/reminder actually get listed depends on
            # ambiguous "CLI defaults", so pin it to "all" instead of relying
            # on that.
            skills="all",
            # Observed 2026-08-15: asked to "remind me in 3 minutes", the
            # model reached for CronCreate (claude.ai routines) instead of
            # the reminder skill -- a routine re-invokes a claude.ai session
            # later, which nothing on this device is watching, so the
            # reminder silently never arrives. Nothing here needs a routine
            # or a wakeup schedule (this is a one-shot voice/Telegram
            # backend, not an agent that keeps running), so block both
            # outright rather than relying on the system-prompt hint alone.
            disallowed_tools=["CronCreate", "ScheduleWakeup"],
        )
        self._mgr = ClaudeSDKClient(options=options)
        self._client = await self._mgr.__aenter__()

    def send(self, text: str) -> str:
        logger.info("Sending to Claude Code: %s", text)
        future = asyncio.run_coroutine_threadsafe(self._query(text), self._loop)
        try:
            return future.result(timeout=_HARD_TIMEOUT_S)
        except concurrent.futures.TimeoutError as exc:
            # _query() itself never got the chance to notice and raise
            # LlmTimeoutError below (that only fires on a stall *between*
            # messages) -- this is the outer "never finished at all" net, so
            # interrupt from out here instead.
            logger.warning(
                "Claude Code turn exceeded the %ss hard cap -- interrupting.",
                _HARD_TIMEOUT_S,
            )
            self.cancel()
            raise LlmTimeoutError(f"No reply within {_HARD_TIMEOUT_S}s") from exc

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
        responses = self._client.receive_response()
        while True:
            try:
                msg = await asyncio.wait_for(anext(responses), timeout=_ACTIVITY_TIMEOUT_S)
            except StopAsyncIteration:
                break
            except TimeoutError as exc:
                # Nothing streamed in a while -- likely stuck (e.g. the
                # timeout that used to show up as an uncaught TimeoutError
                # deep in app.py's _respond thread, silently wedging the
                # whole app in "thinking" -- see docs/backlog.md). Best-effort
                # interrupt so the client is usable again for the next turn
                # before handing the failure up to App, which resets state
                # and tells the user, rather than just re-raising and letting
                # the handler thread die.
                logger.warning(
                    "Claude Code stalled: no activity for %ss -- interrupting.",
                    _ACTIVITY_TIMEOUT_S,
                )
                try:
                    await asyncio.wait_for(self._client.interrupt(), timeout=10)
                except Exception:
                    logger.warning("Interrupt after stall failed", exc_info=True)
                raise LlmTimeoutError(f"No activity for {_ACTIVITY_TIMEOUT_S}s") from exc

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
