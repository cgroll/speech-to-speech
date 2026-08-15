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

If a message consists of just a URL (little or no other text, most often via
Telegram), use the `bookmark` skill on it right away — don't just discuss or
summarize it, save it, then confirm briefly with the generated title/tags.

For time-based reminders ("remind me in 10 minutes to...", "ping me at 5pm
about..."), always use the `reminder` skill (schedules a real Telegram
message via Google Cloud Tasks). Never use a built-in scheduled-task/routine/
cron feature for this — nothing is watching claude.ai on this device, so a
routine would silently never reach the user.
"""


class AgentConversation(Protocol):
    """Contract both backends implement. `resume` (a past session id) is a
    constructor-time argument on each concrete class, not part of this
    Protocol, since it's only ever passed once at construction -- see
    create_conversation()."""

    def send(self, text: str) -> str: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


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
    image delivery shouldn't have to special-case which agent it picked."""
    workspace = workspace or str(DEFAULT_WORKSPACE)
    if agent == AGENT_CLAUDE:
        from speech_to_speech.llm import ClaudeCodeConversation

        return ClaudeCodeConversation(resume=resume, workspace=workspace, on_image=on_image)
    if agent == AGENT_PI:
        from speech_to_speech.pi_agent import PiAgentConversation

        return PiAgentConversation(resume=resume, workspace=workspace)
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
