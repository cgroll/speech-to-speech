"""Shared agent-backend plumbing (docs/backlog.md, "Mehrere Agent-Backends").

Both LLM backends -- llm.py's Claude Agent SDK client and pi_agent.py's Pi
Coding Agent subprocess wrapper -- implement the same tiny send/cancel/close
contract, so `App` can hold either behind one attribute without caring which
one it got. This module is the shared bit: the contract itself, the system
prompt both backends are told to follow, the small id/label registry, and
the factory `App` uses instead of constructing either class directly.

Deliberately no backend-specific imports at module load time (see
create_conversation) -- choosing one backend shouldn't pay for pulling in
the other's SDK/deps (claude_agent_sdk vs. just a `pi` subprocess).
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from speech_to_speech.config import DEFAULT_WORKSPACE

logger = logging.getLogger(__name__)

# The system prompt both backends follow is composed per conversation from a
# shared, mode-neutral base plus one output-formatting block chosen by the
# `voice_output` flag -- see system_prompt_for(). The formatting rules can't be
# one-size-fits-all: a spoken reply must avoid markdown/code/lists and stay
# short, but a Telegram text reply actively wants them. Which surface is which
# is fixed per conversation (App = always spoken, Telegram = text), so the
# choice is made once at create_conversation() rather than per turn.

# Tool access, destructive-action safety, and the skill triggers -- true
# regardless of how the reply is delivered.
SYSTEM_PROMPT_BASE = """\
You have full tool access (Bash, file read/write, web search, etc.) with all
permission checks bypassed. Before executing any command that is destructive or
hard to reverse — deleting files, overwriting data, pushing to remote — pause and
ask the user for explicit confirmation, since there is no automated approval UI.

If a message consists of just a URL (little or no other text, most often via
Telegram), use the `bookmark` skill on it right away — don't just discuss or
summarize it, save it, then confirm briefly with the generated title/tags.

For time-based reminders ("remind me in 10 minutes to...", "ping me at 5pm
about..."), always use the `reminder` skill (schedules a real Telegram
message via Google Cloud Tasks). Never use a built-in scheduled-task/routine/
cron feature for this — nothing is watching claude.ai on this device, so a
routine would silently never reach the user."""

# Appended when the reply is read aloud via TTS (voice_output=True): the App's
# spoken dialog and the cockpit's voice round-trip. Must keep answers speakable.
VOICE_FORMATTING = """\
You are a voice assistant: your response is read aloud via text-to-speech, so it
must be speakable.
- Answer in plain spoken prose. No markdown, no bullet or numbered lists, no
  tables, no code blocks, no inline code, no URLs, no emoji.
- Be brief. A spoken answer should rarely exceed a few sentences unless the user
  explicitly asks for detail — summarize rather than dump everything.
- Say numbers and symbols as words ("fifty percent", not "50%"), and describe
  code or commands in words instead of quoting them verbatim."""

# Appended when the reply is delivered as text (voice_output=False): Telegram.
# Normal chat formatting is welcome there.
TEXT_FORMATTING = """\
Your response is delivered as a text chat message, so normal formatting is fine:
use markdown, code blocks, and lists wherever they make the answer clearer."""


def system_prompt_for(voice_output: bool) -> str:
    """The full system prompt for a conversation whose replies are spoken
    (voice_output=True) or shown as text (False). The formatting block leads so
    it's the most salient instruction, with the shared base rules after it."""
    formatting = VOICE_FORMATTING if voice_output else TEXT_FORMATTING
    return f"{formatting}\n\n{SYSTEM_PROMPT_BASE}"


class AgentConversation(Protocol):
    """Contract both backends implement. `resume` (a past session id) is a
    constructor-time argument on each concrete class, not part of this
    Protocol, since it's only ever passed once at construction -- see
    create_conversation()."""

    def send(self, text: str) -> str: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


# -- Output categories (docs/specs/thinking-channel-and-stop-marker.md) ----
# Every piece of output a backend produces during one send() call falls into
# one of three categories. "response" isn't a constant below -- it's never
# pushed through `on_output`, it's simply send()'s own return value, atomic
# as decided in docs/specs/core-dialog-loop.md section 3/7 (no streaming of
# the final answer). The other two *are* pushed live, via `on_output`, as
# they're produced, before send() returns:
CATEGORY_THINKING = "thinking"  # the agent reasoning out loud / preliminary
# text that turns out not to be the turn's last message (see llm.py/
# pi_agent.py: only the very last assistant message's text is "response",
# everything earlier gets reclassified into this category once a later
# message shows up).
CATEGORY_OTHER = "other"  # catch-all: tool calls/results today, and
# whatever block/event type neither backend's current code knows how to
# name more specifically yet.

# Per-category delivery rule, decided 2026-10-02: voice output (App/
# cockpit's TTS) is wired to *only* ever receive send()'s return value
# (the response), never `on_output` -- that rule lives structurally in
# app.py (_speak() is only ever called with `reply`), not here, but is
# recorded here since this is the module that defines the categories it
# depends on. Text-heavy surfaces (cockpit chat pane) show `on_output`
# categories live and visually distinct from the response; see cockpit.py.


class LlmTimeoutError(Exception):
    """Raised by a backend's send() when a turn is stuck rather than just
    slow -- e.g. llm.py's Claude backend gives up after a stretch of no
    streamed activity at all. Shared here (not defined in llm.py) so App can
    catch one type regardless of which backend is active, per this module's
    job as the common contract both backends implement. The backend is
    expected to have already made a best-effort attempt to interrupt/kill
    whatever was stuck before raising, so the conversation is left usable
    for the next turn."""


# -- Backend registry -------------------------------------------------------
# Ids used everywhere a backend needs naming: the --agent CLI flag, the
# cockpit's new-session/session-list pickers, and the tag stored alongside
# each session so "Frühere Sessions" knows which backend to resume it with.
AGENT_CLAUDE = "claude"
AGENT_PI = "pi"

AGENT_LABELS = {
    AGENT_CLAUDE: "Claude Code",
    AGENT_PI: "Pi",
}

DEFAULT_AGENT = AGENT_CLAUDE


def create_conversation(
    agent: str,
    resume: str | None = None,
    workspace: str | None = None,
    on_image: Callable[[str, str], None] | None = None,
    on_output: Callable[[str, str], None] | None = None,
    on_background_result: Callable[[str, str], None] | None = None,
    voice_output: bool = False,
) -> AgentConversation:
    """Instantiates the right backend client for `agent` (one of
    AGENT_LABELS' keys). Imports are lazy so picking one backend doesn't
    import the other's dependencies.

    `workspace` (docs/backlog.md, "Mehrere Agent-Backends", point 2) sets
    where the agent's file/tool access is rooted -- Claude SDK's `cwd`, Pi
    subprocess's `cwd`. Defaults to DEFAULT_WORKSPACE if not given; callers
    that let a user pick one (cockpit, Telegram bot) should already have
    run it through resolve_workspace() below.

    `on_image`, if given, wires up the show_image tool (image_tool.py) so
    the agent can push an image to the caller's chat/UI -- Claude-only for
    now (see image_tool.py's docstring for why), silently ignored for the
    Pi backend rather than raising, since a caller that always wires up
    image delivery shouldn't have to special-case which agent it picked.

    `on_output`, if given, is called `(category, text)` for every piece of
    non-response output a backend produces during a send() call -- currently
    CATEGORY_THINKING or CATEGORY_OTHER (see above) -- as it's produced, i.e.
    *before* send() returns with the final response. None means the caller
    doesn't want these at all (the backend just discards them, same as
    before this existed); callers that do pass one still always get the
    response itself as send()'s plain return value, unchanged.

    `on_background_result`, if given, is called `(source, text)` for a
    result that arrives with no send() call waiting on it at all -- the
    agent reporting back on its own, e.g. a backgroundable task Claude
    started earlier that only finished after that turn's own reply was
    already returned (docs/specs/background-channel.md). Claude-only for
    now: Pi has no equivalent backgroundable-task primitive (confirmed via
    its own docs -- "It intentionally does not include built-in ...
    background bash"), so this is silently ignored for AGENT_PI, same
    "accepted but no-op for a backend that can't do it" treatment as
    `on_image` above.

    `voice_output` picks the reply-formatting half of the system prompt
    (system_prompt_for): True for conversations whose replies are spoken (the
    App's dialog and cockpit), False for text-delivered ones (Telegram). Set
    once here since the surface -- and thus how replies come out -- is fixed
    for the life of the conversation."""
    workspace = workspace or str(DEFAULT_WORKSPACE)
    if agent == AGENT_CLAUDE:
        from speech_to_speech.llm import ClaudeCodeConversation

        return ClaudeCodeConversation(
            resume=resume,
            workspace=workspace,
            on_image=on_image,
            on_output=on_output,
            on_background_result=on_background_result,
            voice_output=voice_output,
        )
    if agent == AGENT_PI:
        from speech_to_speech.pi_agent import PiAgentConversation

        # on_background_result is deliberately not passed through -- see
        # this function's docstring for why (no equivalent in Pi today).
        return PiAgentConversation(
            resume=resume, workspace=workspace, on_output=on_output, voice_output=voice_output
        )
    raise ValueError(f"Unknown agent backend: {agent!r} (known: {sorted(AGENT_LABELS)})")


def resolve_workspace(path: str) -> str:
    """Validates and normalizes a user-supplied workspace path (cockpit
    folder picker, Telegram `/workspace <path>`): expands `~` and resolves
    to an absolute path, raising ValueError (caught by the caller and shown
    to the user) if it doesn't exist or isn't a directory -- surfaced back
    right away instead of silently falling back to the old workspace."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"not a directory: {resolved}")
    return str(resolved)


def workspace_instruction(workspace: str) -> str:
    """Soft steering to combine with the hard `cwd` confinement each backend
    sets up itself: tells the agent in its own words where its workspace is,
    per docs/backlog.md's "Instruktion im System-Prompt ... kombiniert mit
    der harten Durchsetzung" idea."""
    return (
        f"Your workspace for this session is {workspace}. Treat it as your "
        "working directory for file and tool access, and stay within it "
        "unless explicitly asked to work elsewhere."
    )
