"""LLM backend: the Pi Coding Agent (`@earendil-works/pi-coding-agent`, CLI
`pi` -- see ~/research/pi-test for the project this pattern was first tried
in). Unlike the Claude Agent SDK, Pi has no Python client: it's invoked as a
subprocess per turn instead of held open as a persistent object.

Conversation continuity across turns comes from `--session-id`: passing the
same id on every call tells pi to load and continue that session's own
on-disk JSONL transcript under ~/.pi/agent/sessions/<project>/, creating it
on the first call -- see sessions.py for the reader side of that same file.
This re-establishes a fresh subprocess each turn rather than keeping a
client connection open like ClaudeCodeConversation does, but presents the
same send()/cancel()/close() surface (agent_backend.AgentConversation) so
`App` doesn't need to care which backend it's talking to.

`pi --print --mode json` streams newline-delimited JSON events; verified
empirically (2026-08-12) that tool calls (bash/read/etc.) execute without an
interactive approval prompt in this mode -- matching the Claude backend's
`permission_mode="bypassPermissions"`, so no extra flag is needed here for
that. The event we care about is the final `agent_end`, whose `messages[-1]`
is the assistant's last message of the turn; its `text`-type content blocks
are the actual reply (thinking/toolCall/toolResult blocks are the "reasoning
out loud" and tool-use steps in between, not the answer -- same distinction
docs/backlog.md's "Sprachausgabe liest Zwischenschritte" entry is about for
the Claude backend).
"""

import json
import logging
import subprocess
import threading
import uuid

from speech_to_speech.agent_backend import SYSTEM_PROMPT, workspace_instruction
from speech_to_speech.config import DEFAULT_WORKSPACE

logger = logging.getLogger(__name__)

PI_BIN = "pi"

# How long a single turn (subprocess call) may run before we give up on it.
# Generous on purpose -- tool-heavy turns (web search, multi-file edits) can
# take a while, and a stuck call is otherwise indistinguishable from a slow
# one until this fires.
TURN_TIMEOUT_S = 300


class PiAgentConversation:
    def __init__(self, resume: str | None = None, workspace: str | None = None) -> None:
        # `resume` is a past session_id (see sessions.py) to continue;
        # otherwise a fresh one, handed to `pi --session-id` on every call
        # of this instance so all turns land in the same on-disk session.
        self.session_id = resume or str(uuid.uuid4())
        # `workspace` (docs/backlog.md, "Mehrere Agent-Backends", point 2)
        # becomes the subprocess's cwd below -- note this only confines
        # *this* process's default working directory, not a hard sandbox:
        # Pi's own guardrails extension (~/.pi/agent/extensions/
        # guardrails.json) is what actually enforces path access, and as of
        # writing it's scoped to ~/.agents and ~/research globally, wider
        # than any single session's workspace -- picking a workspace outside
        # those two won't additionally be blocked by Pi, just not
        # specifically whitelisted either.
        self.workspace = workspace or str(DEFAULT_WORKSPACE)
        self._proc_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    def send(self, text: str) -> str:
        logger.info("Sending to Pi Agent: %s", text)
        cmd = [
            PI_BIN,
            "--print",
            "--mode",
            "json",
            "--session-id",
            self.session_id,
            "--append-system-prompt",
            SYSTEM_PROMPT + "\n\n" + workspace_instruction(self.workspace),
            text,
        ]
        with self._proc_lock:
            self._proc = subprocess.Popen(
                cmd,
                cwd=self.workspace,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            proc = self._proc

        # Drain stderr on its own thread while we read stdout below --
        # otherwise a chatty stderr (warnings, extension logs) can fill its
        # pipe buffer and deadlock both sides once stdout blocks too.
        stderr_lines: list[str] = []
        stderr_thread = threading.Thread(
            target=lambda: stderr_lines.extend(proc.stderr), daemon=True
        )
        stderr_thread.start()

        reply = ""
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "agent_end":
                    messages = event.get("messages") or []
                    if messages and messages[-1].get("role") == "assistant":
                        reply = "".join(
                            block.get("text", "")
                            for block in messages[-1].get("content", [])
                            if block.get("type") == "text"
                        ).strip()
        finally:
            proc.stdout.close()
            try:
                returncode = proc.wait(timeout=TURN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                returncode = proc.wait()
            stderr_thread.join(timeout=5)
            with self._proc_lock:
                self._proc = None

        # -15/-9: killed by our own cancel()/timeout handling above -- not a
        # real failure, just means the reply (if any) is a partial one that
        # App discards anyway once its interrupt Event is set.
        if returncode not in (0, -9, -15) and not reply:
            logger.warning(
                "pi exited with code %s: %s", returncode, "".join(stderr_lines)[:500]
            )
        logger.info("Pi Agent reply: %s", reply[:200])
        return reply

    def cancel(self) -> None:
        """Best-effort interrupt of an in-flight turn (e.g. on barge-in):
        terminates the subprocess backing the current send() call, if any.
        Fire-and-forget like ClaudeCodeConversation.cancel() -- send()'s own
        wait() above picks up the resulting exit and just returns whatever
        partial reply (usually none) it had; App discards it once its
        interrupt Event is set, so there's nothing further to coordinate
        here."""
        with self._proc_lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def close(self) -> None:
        """No persistent process/client to tear down between turns (unlike
        ClaudeCodeConversation) -- just make sure nothing is left running."""
        self.cancel()
