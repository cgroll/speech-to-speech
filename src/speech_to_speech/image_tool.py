"""`show_image` -- a custom in-process SDK tool (docs/backlog.md, "Bilder an
den Nutzer senden") that lets the Claude backend push an image file to
whichever channel the current session is running in (Telegram chat, Gradio
cockpit), rather than the agent only being able to mention a file path in
text the user can't see.

Claude-only for now: the SDK's custom-tool mechanism (`@tool` +
`create_sdk_mcp_server`, both in-process) is a natural fit, but Pi runs each
turn as a fresh subprocess with no equivalent in-process callback path --
giving Pi the same capability would need an actual MCP server bridging back
into this process, not just a Python closure. See llm.py/agent_backend.py
for where this is wired in only for ClaudeCodeConversation.

One tool instance per session, not a module-level singleton: `on_image` is a
closure over that session's delivery destination (which chat, which App),
built fresh by make_show_image_tool() each time a ClaudeCodeConversation
starts -- mirrors the SDK's own "server with application state access"
pattern (see create_sdk_mcp_server's docstring).
"""

import logging
import mimetypes
from collections.abc import Callable
from pathlib import Path

from claude_agent_sdk import SdkMcpTool, tool

logger = logging.getLogger(__name__)

# Appended to the system prompt only when a show_image tool is actually
# wired in (i.e. only for Claude sessions with a delivery channel) -- kept
# here, next to the tool it describes, rather than in agent_backend.py's
# shared SYSTEM_PROMPT, so the two can't drift out of sync and so a Pi
# session (which never gets the tool) never sees a tool it doesn't have.
SHOW_IMAGE_INSTRUCTION = (
    "If you create or find an image relevant to your answer, use the show_image "
    "tool to display it to the user instead of just describing it or mentioning "
    "its file path -- the user cannot see file paths you write in your reply."
)

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to the image file, relative to your workspace or absolute.",
        },
        "caption": {
            "type": "string",
            "description": "Optional short caption shown alongside the image.",
        },
    },
    "required": ["path"],
}


def make_show_image_tool(
    workspace: str, on_image: Callable[[str, str], None]
) -> SdkMcpTool:
    """Builds a show_image tool bound to this session's workspace (for
    resolving relative paths) and delivery callback `on_image(path, caption)`
    -- called synchronously with the resolved absolute path once validated;
    the caller is responsible for getting it to the right chat/UI (and for
    being thread-safe/non-blocking, since this runs on the SDK's own
    background event-loop thread, not the caller's)."""

    @tool("show_image", "Displays an image to the user in their current chat.", _INPUT_SCHEMA)
    async def show_image(args: dict) -> dict:
        raw_path = args.get("path", "")
        caption = (args.get("caption") or "").strip()

        resolved = Path(raw_path).expanduser()
        if not resolved.is_absolute():
            resolved = Path(workspace) / resolved
        resolved = resolved.resolve()

        if not resolved.is_file():
            return {
                "content": [{"type": "text", "text": f"No such file: {resolved}"}],
                "is_error": True,
            }
        mime, _ = mimetypes.guess_type(str(resolved))
        if not mime or not mime.startswith("image/"):
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Not an image file (detected {mime or 'unknown type'}): {resolved}",
                    }
                ],
                "is_error": True,
            }

        try:
            on_image(str(resolved), caption)
        except Exception as exc:  # noqa: BLE001 - reported to the agent, not fatal
            logger.exception("show_image delivery failed")
            return {
                "content": [{"type": "text", "text": f"Failed to show image: {exc}"}],
                "is_error": True,
            }
        return {"content": [{"type": "text", "text": f"Shown to the user: {resolved.name}"}]}

    return show_image
