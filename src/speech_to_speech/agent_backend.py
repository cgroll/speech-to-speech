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
from typing import Protocol

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


def create_conversation(agent: str, resume: str | None = None) -> AgentConversation:
    """Instantiates the right backend client for `agent` (one of
    AGENT_LABELS' keys). Imports are lazy so picking one backend doesn't
    import the other's dependencies."""
    if agent == AGENT_CLAUDE:
        from speech_to_speech.llm import ClaudeCodeConversation

        return ClaudeCodeConversation(resume=resume)
    if agent == AGENT_PI:
        from speech_to_speech.pi_agent import PiAgentConversation

        return PiAgentConversation(resume=resume)
    raise ValueError(f"Unknown agent backend: {agent!r} (known: {sorted(AGENT_LABELS)})")
