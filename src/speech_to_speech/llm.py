"""LLM backend: Claude Code Agent SDK with full tool capabilities.

Runs a persistent ClaudeSDKClient in a background asyncio event loop so the
synchronous send() interface that App expects is preserved unchanged.

Message dispatch (2026-10-03 restructuring, docs/specs/background-channel.md):
a single `_dispatch_loop()` reads `self._client.receive_messages()`
continuously for the whole lifetime of the client -- not just while a
send() call is waiting on a reply, which is what the previous
per-call-only draining could not see past. This matters specifically for
Claude's own backgroundable tasks (a bash/tool call the model chooses to
keep running past the turn that started it): their lifecycle shows up as
further messages on the *same* stream (TaskStartedMessage ->
TaskProgressMessage* -> TaskUpdatedMessage/TaskNotificationMessage), which
can arrive well after the turn that kicked them off has already returned
its reply. `_route()` is the dispatcher: while a turn is in flight (i.e.
`_query()` is awaiting its own reply), everything -- including task
lifecycle messages -- flows into that turn's own queue, same as before
this change. Once nothing is waiting, a *terminal* task message instead
goes to `on_background_result`, the new push channel a caller (App,
telegram_bot) wires up to actually say/show it -- this is the mechanism
behind "the agent sets a timer and reports back on its own".
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
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TERMINAL_TASK_STATUSES,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
)

from speech_to_speech.agent_backend import (
    CATEGORY_OTHER,
    CATEGORY_THINKING,
    LlmTimeoutError,
    system_prompt_for,
    workspace_instruction,
)
from speech_to_speech.config import DEFAULT_WORKSPACE
from speech_to_speech.image_tool import SHOW_IMAGE_INSTRUCTION, make_show_image_tool

logger = logging.getLogger(__name__)

# The four message types the CLI emits for a backgroundable task's
# lifecycle (claude_agent_sdk's own Task*Message dataclasses, all
# SystemMessage subclasses). _route() below treats the latter two as
# "terminal" when their own `status` is one of TERMINAL_TASK_STATUSES.
_TASK_MESSAGE_TYPES = (
    TaskStartedMessage,
    TaskProgressMessage,
    TaskUpdatedMessage,
    TaskNotificationMessage,
)


def _describe_task_message(msg: object) -> str:
    """Human-readable line for a task-lifecycle message, used when one
    arrives *during* an active turn (routed to CATEGORY_OTHER via _emit(),
    same as a tool call/result) -- not used for the background-delivery
    path, which prefers TaskNotificationMessage's own `summary` field
    instead (see ClaudeCodeConversation._deliver_background())."""
    if isinstance(msg, TaskStartedMessage):
        return f"⏳ Hintergrundaufgabe gestartet: {msg.description}"
    if isinstance(msg, TaskProgressMessage):
        return f"⏳ {msg.description}: läuft noch"
    if isinstance(msg, TaskUpdatedMessage):
        return f"⏳ Hintergrundaufgabe {msg.task_id}: Status {msg.status}"
    if isinstance(msg, TaskNotificationMessage):
        return f"✅ Hintergrundaufgabe beendet ({msg.status}): {msg.summary}"
    return str(msg)

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
        on_output: Callable[[str, str], None] | None = None,
        on_background_result: Callable[[str, str], None] | None = None,
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
        # agent_backend.CATEGORY_THINKING/CATEGORY_OTHER, pushed live as
        # blocks stream in -- see _query()'s classification below. None
        # means the caller doesn't want these (e.g. Telegram, for now).
        self._on_output = on_output
        # Background-task completion delivery (see module docstring):
        # called `(description, text)` for a *terminal* task-lifecycle
        # message that arrives while no send() call is waiting on a reply
        # -- i.e. the agent reporting back on its own. None means the
        # caller doesn't want this (message is just dropped after bookkeeping,
        # see _route()).
        self._on_background_result = on_background_result
        # Whether this conversation's replies get spoken -- picks the voice vs.
        # text formatting half of the system prompt (set once in _start()).
        self._voice_output = voice_output
        # task_id -> description, populated on TaskStartedMessage and
        # popped once that task reaches a terminal status (TaskUpdatedMessage
        # or TaskNotificationMessage, see _route()) -- used to label a
        # background result with *what* it was, since the terminal message
        # itself doesn't always repeat the description.
        self._active_tasks: dict[str, str] = {}
        # Set for the duration of one _query() call (None otherwise): the
        # queue _route() feeds while a turn is actively being awaited. Only
        # ever touched from this instance's own event-loop thread (set in
        # _query(), read in _route(), both running on self._loop), so no
        # lock is needed despite being written from two call sites.
        self._turn_queue: asyncio.Queue | None = None
        self._dispatch_task: asyncio.Task | None = None
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
        # Starts now, for the whole remaining lifetime of this client --
        # deliberately not scoped to send()/_query() (see module docstring):
        # a backgroundable task's completion can otherwise arrive with
        # nothing left listening for it.
        self._dispatch_task = self._loop.create_task(self._dispatch_loop())

    async def _dispatch_loop(self) -> None:
        """Runs for as long as the client is connected, reading every
        message the CLI emits. Replaces the old per-_query() draining of
        `receive_response()` -- that only ever listened while a send() call
        was waiting on its reply, which is exactly the gap for a
        backgroundable task whose completion arrives after its owning turn
        already returned (see module docstring)."""
        try:
            async for message in self._client.receive_messages():
                self._route(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Claude Code dispatch loop crashed -- live turns and "
                "background-task delivery on this client will stop working "
                "until the client is recreated (e.g. a new session)."
            )

    def _route(self, message: object) -> None:
        """Dispatches one message from the stream: into the currently
        waiting turn's queue if `_query()` is actively waiting on one, or
        -- for a *terminal* task-lifecycle message with nobody waiting --
        to `_deliver_background()` instead. Runs entirely on `self._loop`'s
        thread (called only from `_dispatch_loop()`), same as `_turn_queue`
        being set/cleared only from `_query()` on that same thread, so the
        two never race despite not sharing a lock."""
        if isinstance(message, TaskStartedMessage):
            self._active_tasks[message.task_id] = message.description

        terminal = (
            isinstance(message, (TaskUpdatedMessage, TaskNotificationMessage))
            and getattr(message, "status", None) in TERMINAL_TASK_STATUSES
        )

        if self._turn_queue is not None:
            # A turn is actively being awaited -- everything (including
            # task-lifecycle messages) flows through it, same as before
            # this restructuring; _query() classifies task messages into
            # CATEGORY_OTHER via _describe_task_message().
            self._turn_queue.put_nowait(message)
        elif terminal:
            # Nobody is waiting and this task just finished -- the exact
            # "agent reports back on its own" case this was built for.
            self._deliver_background(message)
        # else: non-terminal task chatter (started/progress) with no turn
        # waiting and no terminal status yet -- nothing to deliver, the
        # TaskStartedMessage bookkeeping above is all that's needed from it.

        if terminal:
            self._active_tasks.pop(getattr(message, "task_id", None), None)

    def _deliver_background(self, message: object) -> None:
        if self._on_background_result is None:
            return
        description = self._active_tasks.get(getattr(message, "task_id", None), "Hintergrundaufgabe")
        summary = getattr(message, "summary", None)
        text = (summary or f"{description} ({getattr(message, 'status', 'fertig')})").strip()
        if not text:
            return
        logger.info("Background task result (%s): %s", description, text[:200])
        self._on_background_result(description, text)

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
        runs nothing should still be calling into the old instance.

        Cancels `_dispatch_loop()` first -- it now outlives any single
        send() call (see module docstring), so nothing else would ever stop
        it on its own once the underlying client goes away."""

        async def _stop() -> None:
            if self._dispatch_task is not None:
                self._dispatch_task.cancel()
                try:
                    await self._dispatch_task
                except (asyncio.CancelledError, Exception):
                    pass
            await self._mgr.__aexit__(None, None, None)

        try:
            asyncio.run_coroutine_threadsafe(_stop(), self._loop).result(timeout=10)
        except Exception:
            logger.warning("Error closing Claude client during reset", exc_info=True)
        self._loop.call_soon_threadsafe(self._loop.stop)

    def _emit(self, category: str, text: str) -> None:
        text = text.strip()
        if text and self._on_output is not None:
            self._on_output(category, text)

    async def _query(self, text: str) -> str:
        await self._client.query(text)
        # The response (agent_backend's atomic, non-streamed Antwort-Kanal)
        # is built with one-message lookahead: an AssistantMessage's text is
        # only *provisionally* the final reply until we see whether another
        # AssistantMessage follows it in this same turn. If one does, this
        # message wasn't the last word after all -- demote it to
        # CATEGORY_THINKING (emitted live, right then) and the new message's
        # text becomes the provisional reply instead. Whatever's still
        # pending when the stream ends is the real, final response -- same
        # "only the last message's text counts" rule
        # docs/specs/thinking-channel-and-stop-marker.md already settled on,
        # just implemented incrementally instead of only after the fact.
        #
        # Messages themselves now come from `_turn_queue`, fed by the
        # always-running `_dispatch_loop()`/`_route()` (see module
        # docstring) instead of iterating `receive_response()` directly --
        # that's what lets a backgroundable task's completion still reach
        # `on_background_result` on turns *other* than this one, or while
        # idle, rather than being read (and silently dropped) only here.
        pending_text: str | None = None
        last_result: str | None = None
        queue: asyncio.Queue = asyncio.Queue()
        self._turn_queue = queue
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=_ACTIVITY_TIMEOUT_S)
                except TimeoutError as exc:
                    # Nothing streamed in a while -- likely stuck (e.g. the
                    # timeout that used to show up as an uncaught TimeoutError
                    # deep in app.py's _respond thread, silently wedging the
                    # whole app in "thinking" -- see docs/backlog.md). Best-effort
                    # interrupt so the client is usable again for the next turn
                    # before handing the failure up to App, which resets state
                    # and tells the user, rather than just re-raising and letting
                    # the handler thread die. Scoped to *this* wait only --
                    # unlike before, _dispatch_loop() itself has no timeout and
                    # is expected to sit idle for arbitrarily long stretches
                    # between turns.
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
                    message_text_parts: list[str] = []
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            message_text_parts.append(block.text)
                        elif isinstance(block, ThinkingBlock):
                            self._emit(CATEGORY_THINKING, block.thinking)
                        elif isinstance(block, ToolUseBlock):
                            self._emit(CATEGORY_OTHER, f"→ {block.name}({block.input})")
                        else:
                            self._emit(CATEGORY_OTHER, str(block))

                    message_text = "".join(message_text_parts)
                    if message_text.strip():
                        if pending_text is not None:
                            # A later message showed up -- the earlier one
                            # wasn't the last word, demote it now that we know.
                            self._emit(CATEGORY_THINKING, pending_text)
                        pending_text = message_text
                        print(message_text, end="", flush=True)
                elif isinstance(msg, UserMessage):
                    # Tool *results* stream back as a UserMessage, not on the
                    # AssistantMessage that requested them (unlike Pi, see
                    # pi_agent.py) -- same CATEGORY_OTHER bucket either way.
                    content = msg.content if isinstance(msg.content, list) else []
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            self._emit(CATEGORY_OTHER, f"← {block.content}")
                elif isinstance(msg, _TASK_MESSAGE_TYPES):
                    # A backgroundable task's lifecycle, surfacing *during*
                    # this turn (e.g. it both started and finished before
                    # this turn's own ResultMessage) -- shown live same as
                    # any other tool activity. A task that outlives this
                    # turn instead reaches on_background_result once nothing
                    # is waiting anymore (_route()), not here.
                    self._emit(CATEGORY_OTHER, _describe_task_message(msg))
                elif isinstance(msg, ResultMessage):
                    if msg.result:
                        last_result = msg.result
                    # The CLI emits exactly one ResultMessage per turn, and
                    # it's always the last message of that turn -- stop
                    # consuming here instead of relying on the stream ending
                    # (there is no "end" anymore; _dispatch_loop() keeps
                    # running for later turns/background results).
                    break
        finally:
            self._turn_queue = None

        print()
        reply = (pending_text or last_result or "").strip()
        logger.info("Claude Code reply: %s", reply[:200])
        return reply


def _log_if_failed(future: asyncio.Future) -> None:
    exc = future.exception()
    if exc is not None:
        logger.warning("Interrupt failed: %s", exc)
