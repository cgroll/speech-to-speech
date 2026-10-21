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
`App` doesn't need to care which backend it's talking to. One place that
surface is deliberately wider here: `model`/`set_model()` switch the model
mid-session, which costs nothing extra precisely because of that per-turn
subprocess (see PiAgentConversation.set_model); the Claude backend has no
equivalent and ignores the choice (see create_conversation).

`pi --print --mode json` streams newline-delimited JSON events; verified
empirically (2026-08-12) that tool calls (bash/read/etc.) execute without an
interactive approval prompt in this mode -- matching the Claude backend's
`permission_mode="bypassPermissions"`, so no extra flag is needed here for
that.

send() classifies each `message_end` event live as it streams (re-verified
empirically 2026-10-02 against the actual event shapes, see
docs/specs/thinking-channel-and-stop-marker.md): an assistant message's
`text` blocks are the provisional reply, demoted to
`agent_backend.CATEGORY_THINKING` the moment a *later* assistant message
shows up in the same turn (only the last one was ever the real answer);
`thinking` blocks and `toolCall`/`toolResult` messages are pushed to
`on_output` immediately as `CATEGORY_THINKING`/`CATEGORY_OTHER` -- never
part of the reply. Same distinction docs/backlog.md's "Sprachausgabe liest
Zwischenschritte" entry is about for the Claude backend (llm.py).
"""

import json
import logging
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from speech_to_speech.agent_backend import (
    CATEGORY_OTHER,
    CATEGORY_THINKING,
    system_prompt_for,
    workspace_instruction,
)
from speech_to_speech.config import DEFAULT_WORKSPACE, PI_BIN, PI_DEFAULT_MODEL

logger = logging.getLogger(__name__)


# How long a single turn (subprocess call) may run before we give up on it.
# Generous on purpose -- tool-heavy turns (web search, multi-file edits) can
# take a while, and a stuck call is otherwise indistinguishable from a slow
# one until this fires.
TURN_TIMEOUT_S = 300

# -- Available models -------------------------------------------------------
# `pi --list-models` prints one row per model pi can currently use: every
# built-in model whose provider has cached credentials (~/.pi/agent/auth.json,
# here: GitHub Copilot) plus every custom provider declared in
# ~/.pi/agent/models.json (here: the E.ON endpoint). `--offline` is deliberate
# -- without it pi first tries to reach each provider over the network, which
# turns a 2s local read into a multi-second wait behind the corporate proxy, and
# the list could only ever be as fresh as those cached credentials anyway.
# Re-read on demand (the cockpit's "Model-Liste aktualisieren") rather than
# on a timer, so the picker stays responsive; PI_MODELS_CACHE_TTL_S only bounds
# how long a cached list may be reused by repeated calls.
PI_MODELS_TIMEOUT_S = 30
PI_MODELS_CACHE_TTL_S = 300

_models_cache: tuple[float, list[tuple[str, str]]] = (0.0, [])
_models_cache_lock = threading.Lock()


def _custom_model_names() -> dict[str, str]:
    """`"<provider>/<id>" -> name` for models declared in
    ~/.pi/agent/models.json. pi's --list-models output prints raw ids only, but
    a custom provider can carry a friendlier `name` (the E.ON models do: "Smart
    Router (E.ON)"), which is used here to label the picker. Purely cosmetic:
    anything unreadable about the file (different location, different schema,
    not readable) yields {} and the plain ids are used instead."""
    try:
        raw = json.loads((Path.home() / ".pi" / "agent" / "models.json").read_text())
        return {
            f"{provider}/{model['id']}": model["name"]
            for provider, cfg in raw["providers"].items()
            for model in cfg.get("models", [])
            if model.get("name")
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}


def list_pi_models(force_refresh: bool = False) -> list[tuple[str, str]]:
    """Available models as `("<provider>/<id>", label)` pairs -- what to pass
    as `model` (pi's --model argument) and what to show the user, in that
    order, deliberately not Gradio's (label, value) order since this module
    knows nothing about the cockpit. The cockpit flips them at its own edge.

    Never raises: if `pi` is missing, fails or times out this returns the
    previously cached list (possibly empty, e.g. on a first call that fails),
    so the picker degrades to "empty, but type your own" (its
    allow_custom_value) instead of breaking the cockpit."""
    global _models_cache
    with _models_cache_lock:
        fetched_at, cached = _models_cache
        if cached and not force_refresh and time.monotonic() - fetched_at < PI_MODELS_CACHE_TTL_S:
            return cached

    choices: list[tuple[str, str]] = []
    try:
        proc = subprocess.run(
            [PI_BIN, "--offline", "--list-models"],
            capture_output=True,
            text=True,
            timeout=PI_MODELS_TIMEOUT_S,
            check=False,
        )
        names = _custom_model_names()
        for line in proc.stdout.splitlines():
            # Fixed-width table: `provider model context max-out thinking
            # images`. Only the first two columns matter here; whitespace-split
            # is safe because neither a provider nor a model id contains spaces.
            parts = line.split()
            if len(parts) < 2 or parts[0] == "provider":
                continue
            value = f"{parts[0]}/{parts[1]}"
            choices.append((value, names.get(value, f"{parts[1]} ({parts[0]})")))
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Could not list pi models: %s", exc)

    if not choices:
        logger.warning("pi --list-models returned no models; reusing cached list.")
        choices = cached
    with _models_cache_lock:
        _models_cache = (time.monotonic(), choices)
    return choices


class PiAgentConversation:
    def __init__(
        self,
        resume: str | None = None,
        workspace: str | None = None,
        on_output: Callable[[str, str], None] | None = None,
        voice_output: bool = False,
        model: str | None = None,
    ) -> None:
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
        # agent_backend.CATEGORY_THINKING/CATEGORY_OTHER, pushed live as
        # events stream in -- see send()'s classification below.
        self._on_output = on_output
        # Spoken vs. text reply formatting, appended to Pi's own system prompt
        # on every turn (see send()).
        self._voice_output = voice_output
        # `model` becomes `pi --model <provider>/<id>` on every turn (see
        # send()/set_model()). Public + plain-assignment on purpose: switching
        # it mid-session is supported precisely because there is no persistent
        # pi connection to disturb, only the next subprocess reads it.
        self.model = model or PI_DEFAULT_MODEL
        self._proc_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    def set_model(self, model: str | None) -> None:
        """Switches the model for every turn from the next one on, without
        restarting the session: continuity lives in the on-disk transcript that
        `--session-id` reloads (see module docstring), not in any pi process,
        so a different model simply continues the same conversation. A turn
        already in flight is unaffected -- its command line is built -- and
        finishes on the model it started with. None means "no explicit choice",
        i.e. PI_DEFAULT_MODEL rather than leaving it to pi's own settings, so
        the cockpit always shows which model is actually in use."""
        self.model = model or PI_DEFAULT_MODEL
        logger.info("Pi model set to %s.", self.model)

    def _emit(self, category: str, text: str) -> None:
        text = text.strip()
        if text and self._on_output is not None:
            self._on_output(category, text)

    def send(self, text: str) -> str:
        logger.info("Sending to Pi Agent: %s", text)
        # Read once for the whole turn, so a set_model() landing mid-turn can't
        # half-affect it (and the log/warning below name the same model the
        # command line used).
        model = self.model
        cmd = [
            PI_BIN,
            "--print",
            "--mode",
            "json",
            "--session-id",
            self.session_id,
            # Always passed explicitly rather than falling back to pi's own
            # settings default: the surfaces display App.get_model() as the
            # model in use, which is only honest if it's literally what pi got
            # told (and self.model is never empty -- see set_model()).
            "--model",
            model,
            "--append-system-prompt",
            system_prompt_for(self._voice_output) + "\n\n" + workspace_instruction(self.workspace),
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

        # Same one-message-lookahead approach as llm.py's _query(): a
        # "message_end" assistant message's text is only *provisionally*
        # the final reply until another assistant message follows it in
        # this turn -- if one does, the earlier one gets reclassified to
        # CATEGORY_THINKING (emitted live, right then) instead. Whatever's
        # still pending once the process exits is the real reply -- same
        # "only the last message's text counts" rule as before (previously
        # only checked at `agent_end`/`messages[-1]`), just incremental now
        # so CATEGORY_THINKING/CATEGORY_OTHER pieces surface as they happen
        # instead of being silently dropped.
        pending_text: str | None = None
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type")
                message = event.get("message") or {}
                role = message.get("role")

                if event_type == "message_end" and role == "assistant":
                    message_text_parts: list[str] = []
                    for block in message.get("content", []):
                        block_type = block.get("type")
                        if block_type == "text":
                            message_text_parts.append(block.get("text", ""))
                        elif block_type == "thinking":
                            self._emit(CATEGORY_THINKING, block.get("text", ""))
                        elif block_type == "toolCall":
                            self._emit(
                                CATEGORY_OTHER,
                                f"→ {block.get('name')}({block.get('arguments')})",
                            )
                        else:
                            self._emit(CATEGORY_OTHER, str(block))
                    message_text = "".join(message_text_parts)
                    if message_text.strip():
                        if pending_text is not None:
                            self._emit(CATEGORY_THINKING, pending_text)
                        pending_text = message_text
                elif role == "toolResult":
                    # Tool results are usually their own messages (role=toolResult)
                    # and arrive as message_end events as well.
                    if event_type == "message_end":
                        result_text = "".join(
                            block.get("text", "")
                            for block in message.get("content", [])
                            if block.get("type") == "text"
                        )
                        self._emit(CATEGORY_OTHER, f"← {result_text}")
                elif event_type == "thinking":
                    # Some Pi versions/modes might send thinking events directly
                    self._emit(CATEGORY_THINKING, event.get("text", ""))
                elif event_type == "tool_execution_start":
                    self._emit(CATEGORY_OTHER, f"→ {event.get('name')}")
                elif event_type == "tool_execution_result":
                    self._emit(CATEGORY_OTHER, f"← {event.get('result')}")

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

        reply = (pending_text or "").strip()

        # -15/-9: killed by our own cancel()/timeout handling above -- not a
        # real failure, just means the reply (if any) is a partial one that
        # App discards anyway once its interrupt Event is set.
        if returncode not in (0, -9, -15) and not reply:
            # A typo'd/expired `model` shows up exactly here (pi exits non-zero
            # before producing any event), hence the model is named in the log.
            logger.warning(
                "pi exited with code %s (model %s): %s",
                returncode,
                model,
                "".join(stderr_lines)[:500],
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
