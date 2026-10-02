"""Read-only helpers for listing and loading past agent sessions across both
backends (docs/backlog.md, "Frühere Sessions wieder aufnehmen können" and
"Mehrere Agent-Backends").

Session *persistence* needs no work here for either backend -- both already
write their own on-disk transcripts:
- Claude Agent SDK: JSONL under ~/.claude/projects/<project>/, read via its
  own `list_sessions()` / `get_session_messages()` (see llm.py's `resume=`).
- Pi: JSONL under ~/.pi/agent/sessions/<project>/, one file per session
  named "<timestamp>_<session_id>.jsonl" (see pi_agent.py's `--session-id`).
  No SDK to read it back with, so this module parses those files directly.

This module's job is just: list both, tag each with which backend it
belongs to, merge into one chronological list for the cockpit's "Frühere
Sessions" picker (decision in docs/backlog.md: one shared list, not two),
and reload a picked session's turns for display.

Note both listings include *all* sessions ever run against this project
directory, not just ones started from this app -- e.g. also plain `claude`/
`pi` CLI coding sessions. That's deliberate (no separate storage to
maintain), but means the picker can include sessions that aren't voice
conversations.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import get_session_messages, list_sessions

from speech_to_speech.agent_backend import AGENT_CLAUDE, AGENT_LABELS, AGENT_PI
from speech_to_speech.config import PROJECT_DIR

# How many past sessions the cockpit's picker shows in total (across both
# backends) -- older ones are still on disk and resumable by session_id,
# just not listed here.
MAX_LISTED_SESSIONS = 20

# Synthetic text blocks the CLI/IDE integration injects into user turns
# (slash commands, IDE file-open notices, etc.) -- not something the user
# actually said, so dropped when rebuilding cockpit-display history.
_SYNTHETIC_TEXT_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<ide_opened_file>",
    "<ide_selection>",
    "<system-reminder>",
    "<local-command-stdout>",
)

PI_SESSIONS_ROOT = Path.home() / ".pi" / "agent" / "sessions"

# How many lines of a Pi session file to scan for the first user message
# (used as a display title, mirroring Claude's SDKSessionInfo.first_prompt)
# -- it's always near the top, right after the session/model-change header
# lines, so this is generous headroom rather than a real limit in practice.
_PI_TITLE_SCAN_LINES = 50


@dataclass
class UnifiedSessionInfo:
    agent: str  # AGENT_CLAUDE or AGENT_PI
    session_id: str
    last_modified_ms: float
    title: str


def _pi_project_session_dir(workspace: Path) -> Path:
    """Mirrors pi's own project-directory slug: cwd's path with "/" turned
    into "-", wrapped in "--...--" -- e.g. /home/chris/research/foo becomes
    --home-chris-research-foo--. Verified empirically (2026-08-12) against
    pi's actual output directories; PiAgentConversation always launches `pi`
    with cwd=workspace (see pi_agent.py)."""
    slug = str(workspace).strip("/").replace("/", "-")
    return PI_SESSIONS_ROOT / f"--{slug}--"


def _list_claude_sessions(workspace: Path) -> list[UnifiedSessionInfo]:
    infos = list_sessions(directory=str(workspace), limit=MAX_LISTED_SESSIONS)
    result = []
    for info in infos:
        title = info.custom_title or info.summary or info.first_prompt or info.session_id
        result.append(
            UnifiedSessionInfo(
                agent=AGENT_CLAUDE,
                session_id=info.session_id,
                last_modified_ms=info.last_modified,
                title=title,
            )
        )
    return result


def _list_pi_sessions(workspace: Path) -> list[UnifiedSessionInfo]:
    session_dir = _pi_project_session_dir(workspace)
    if not session_dir.is_dir():
        return []

    result = []
    for path in session_dir.glob("*.jsonl"):
        session_id, title = _read_pi_session_header(path)
        if session_id is None:
            continue
        result.append(
            UnifiedSessionInfo(
                agent=AGENT_PI,
                session_id=session_id,
                last_modified_ms=path.stat().st_mtime * 1000,
                title=title or session_id,
            )
        )
    result.sort(key=lambda info: info.last_modified_ms, reverse=True)
    return result[:MAX_LISTED_SESSIONS]


def _read_pi_session_header(path: Path) -> tuple[str | None, str | None]:
    """Returns (session_id, first_user_prompt) by scanning the first few
    lines of a Pi session file -- the session id from the `"type":"session"`
    header line, the title from the earliest user text turn after it."""
    session_id = None
    title = None
    try:
        with path.open() as f:
            for i, line in enumerate(f):
                if i >= _PI_TITLE_SCAN_LINES:
                    break
                event = _json_loads_safe(line)
                if event is None:
                    continue
                if event.get("type") == "session" and session_id is None:
                    session_id = event.get("id")
                elif event.get("type") == "message" and title is None:
                    message = event.get("message") or {}
                    if message.get("role") == "user":
                        text = _extract_pi_text(message)
                        if text:
                            title = text
                if session_id is not None and title is not None:
                    break
    except OSError:
        return None, None
    return session_id, title


def _json_loads_safe(line: str) -> dict | None:
    import json

    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def list_recent_sessions(workspace: str, agent: str | None = None) -> list[UnifiedSessionInfo]:
    """Both backends' sessions for the given workspace, newest first,
    capped at MAX_LISTED_SESSIONS combined (not per backend). Optionally
    filtered by agent."""
    workspace_path = Path(workspace).expanduser().resolve()
    merged = []
    if agent is None or agent == AGENT_CLAUDE:
        merged += _list_claude_sessions(workspace_path)
    if agent is None or agent == AGENT_PI:
        merged += _list_pi_sessions(workspace_path)
    merged.sort(key=lambda info: info.last_modified_ms, reverse=True)
    return merged[:MAX_LISTED_SESSIONS]


def session_label(info: UnifiedSessionInfo) -> str:
    when = datetime.fromtimestamp(info.last_modified_ms / 1000).strftime("%d.%m. %H:%M")
    agent_label = AGENT_LABELS.get(info.agent, info.agent)
    return f"[{agent_label}] {when} — {info.title}"


def session_choices(workspace: str, agent: str | None = None) -> list[tuple[str, str]]:
    """(label, value) pairs, ready for gr.Radio's `choices`. `value` packs
    both the backend and the id ("claude:<id>" / "pi:<id>") since Gradio's
    choice value is a single string -- App.resume_session() unpacks it."""
    return [
        (session_label(info), f"{info.agent}:{info.session_id}")
        for info in list_recent_sessions(workspace, agent)
    ]


def load_session_history(agent: str, session_id: str) -> list[dict[str, str]]:
    """Rebuilds the cockpit's simple {role, content} turn list from a past
    session's full transcript, dispatched to the right backend's format.
    Text turns only -- tool calls, thinking blocks etc. are part of the
    resumed conversation (the LLM still has that context) but were never
    shown in the cockpit for live turns either, so dropping them here keeps
    resumed history visually consistent with that."""
    if agent == AGENT_CLAUDE:
        return _load_claude_session_history(session_id)
    if agent == AGENT_PI:
        return _load_pi_session_history(session_id)
    raise ValueError(f"Unknown agent backend: {agent!r}")


def _load_claude_session_history(session_id: str) -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    for msg in get_session_messages(session_id, directory=str(PROJECT_DIR)):
        if msg.type not in ("user", "assistant"):
            continue
        text = _extract_text(msg.message)
        if text:
            history.append({"role": msg.type, "content": text})
    return history


def _load_pi_session_history(session_id: str) -> list[dict[str, str]]:
    session_dir = _pi_project_session_dir()
    path = next(session_dir.glob(f"*_{session_id}.jsonl"), None)
    if path is None:
        return []

    history: list[dict[str, str]] = []
    with path.open() as f:
        for line in f:
            event = _json_loads_safe(line)
            if event is None or event.get("type") != "message":
                continue
            message = event.get("message") or {}
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _extract_pi_text(message)
            if text:
                history.append({"role": role, "content": text})
    return history


def _extract_text(message: dict) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        blocks = [content]
    elif isinstance(content, list):
        blocks = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
    else:
        return ""

    parts = [
        block.strip()
        for block in blocks
        if block.strip() and not block.lstrip().startswith(_SYNTHETIC_TEXT_PREFIXES)
    ]
    return "\n".join(parts).strip()


def _extract_pi_text(message: dict) -> str:
    """Same idea as _extract_text() but for Pi's message shape -- content is
    always a list of typed blocks (text/thinking/toolCall/...), never a bare
    string, and there are no synthetic-prefix injections to filter here."""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        block.get("text", "").strip()
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip()
    ]
    return "\n".join(parts).strip()
