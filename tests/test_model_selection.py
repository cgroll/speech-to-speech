"""Choosing which model the Pi backend uses (E.ON vs. GitHub Copilot).

Two separate guarantees, tested at the two levels they live on:

- `App` side: the model is App state that switches *live*, without the
  reset()/resume_session() teardown a backend or workspace change needs --
  because only the Pi backend has the capability at all, and it needs nothing
  but a fresh argv for its next turn. See agent_backend.ModelSwitchable.
- `PiAgentConversation` side: what "switches live" means concretely -- `--model
  <provider>/<id>` in the command line of the *next* `pi` subprocess, with the
  turn already in flight left on the model it started with.

Plus where the cockpit's choices come from (pi_agent.list_pi_models()).
"""

from __future__ import annotations

import io
import json
import subprocess
import threading

import pytest

from speech_to_speech import cockpit, pi_agent
from speech_to_speech.config import PI_DEFAULT_MODEL

from .conftest import Harness, wait_until
from .fakes import FakeAgentConversation, ModelUnswitchableBackend

# A trimmed-down `pi --offline --list-models` run (the real one lists more).
TABLE = """provider        model             context  max-out  thinking  images
eon-ai          qwen-3.8-flash    190K     32K      no        no
eon-ai          smart-router      190K     32K      no        no
github-copilot  claude-sonnet-5   1M       128K     yes       yes
"""

SMART_ROUTER = "eon-ai/smart-router"
COPILOT_SONNET = "github-copilot/claude-sonnet-5"


def _answer(text: str = "fertig") -> str:
    """One `pi --mode json` turn: a single assistant message_end, i.e. the
    shortest stream that makes send() return a non-empty reply."""
    event = {
        "type": "message_end",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }
    return json.dumps(event) + "\n"


def _flag(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


# -- App level: the model is live App state --------------------------------


def test_app_exposes_the_configured_default_model(harness: Harness) -> None:
    assert harness.app.get_model() == PI_DEFAULT_MODEL


def test_app_set_model_switches_live_without_a_new_session(harness: Harness) -> None:
    """The point of the whole feature: a new model from the next turn on, with
    the conversation -- here, the backend instance App reuses across turns --
    untouched and nothing closed or rebuilt behind it."""
    app, llm = harness.app, harness.llm
    app.submit_text("erste Frage")
    assert wait_until(lambda: llm.sent == ["erste Frage"])

    assert app.set_model(COPILOT_SONNET) is True

    app.submit_text("zweite Frage")
    assert wait_until(lambda: llm.sent == ["erste Frage", "zweite Frage"])
    assert llm.model == COPILOT_SONNET
    assert app.get_model() == COPILOT_SONNET
    assert app._llm is llm, "switching the model must not replace the backend"
    assert llm.close_calls == 0, "switching the model must not close the session"
    assert llm.model_calls == [COPILOT_SONNET]


def test_app_treats_a_cleared_choice_as_the_default_model(harness: Harness) -> None:
    """The cockpit's picker is clearable; clearing means "no explicit choice",
    i.e. config's default rather than an absent --model flag whose model no
    surface could display."""
    assert harness.app.set_model(None) is True

    assert harness.app.get_model() == PI_DEFAULT_MODEL
    assert harness.llm.model == PI_DEFAULT_MODEL


def test_app_survives_a_backend_without_model_choice(harness: Harness) -> None:
    """The Claude backend implements no set_model at all (and its model ids
    aren't pi's "provider/id" anyway). App has to report that honestly --
    get_model() None so nothing advertises a model that isn't in use,
    set_model() False so the cockpit can say so -- while still remembering the
    choice for the session that follows."""
    app = harness.app
    app._llm = ModelUnswitchableBackend()

    assert app.set_model(SMART_ROUTER) is False
    assert app.get_model() is None, "Claude is active, so no model is actually in use"
    assert "Modell" not in app.get_stats_text()

    app.reset(agent="pi")
    try:
        assert isinstance(app._llm, pi_agent.PiAgentConversation)
        assert app.get_model() == SMART_ROUTER, "the remembered choice applies"
    finally:
        app._llm.close()


# -- Backend level: what the switch does to pi's argv ----------------------


class FakePopen:
    """Minimal subprocess.Popen stand-in: hands back a canned `pi --mode json`
    stream and records the argv it was built with, which is where send()'s
    model handling is entirely visible."""

    def __init__(self, cmd, stdout_text: str = "", returncode: int = 0):
        self.cmd = list(cmd)
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO("")
        self.returncode = returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def kill(self) -> None:
        pass

    def poll(self) -> int | None:
        return self.returncode


@pytest.fixture
def procs(monkeypatch) -> list[FakePopen]:
    """Patches pi_agent's Popen so every turn records its argv and answers with
    the same one-message reply; the returned list is one entry per turn."""
    recorded: list[FakePopen] = []

    def fake_popen(cmd, **kwargs):
        proc = FakePopen(cmd, _answer())
        recorded.append(proc)
        return proc

    monkeypatch.setattr(pi_agent.subprocess, "Popen", fake_popen)
    return recorded


def test_send_passes_the_model_flag(procs) -> None:
    conv = pi_agent.PiAgentConversation(model=COPILOT_SONNET)

    assert conv.send("hallo") == "fertig"
    assert _flag(procs[0].cmd, "--model") == COPILOT_SONNET


def test_send_defaults_to_the_configured_model(procs) -> None:
    pi_agent.PiAgentConversation().send("hallo")

    assert _flag(procs[0].cmd, "--model") == PI_DEFAULT_MODEL


def test_set_model_changes_the_next_turns_command(procs) -> None:
    conv = pi_agent.PiAgentConversation(model=SMART_ROUTER)
    conv.send("eins")
    conv.set_model(COPILOT_SONNET)
    conv.send("zwei")

    first, second = (proc.cmd for proc in procs)
    assert _flag(first, "--model") == SMART_ROUTER
    assert _flag(second, "--model") == COPILOT_SONNET
    # ... while the conversation itself keeps carrying over -- same session id
    # on both turns, which is exactly *why* no new session is needed here.
    assert _flag(first, "--session-id") == _flag(second, "--session-id")


def test_a_turn_already_in_flight_keeps_its_own_model(monkeypatch) -> None:
    """send() reads the model once, when it builds the command line, so a switch
    that lands mid-turn takes effect from the following turn only -- the reply
    that arrives afterwards is still the original model's."""
    inside = threading.Event()
    release = threading.Event()
    cmds: list[list[str]] = []

    class GatedStdout:
        def __init__(self, text: str):
            self._lines = text.splitlines(keepends=True)

        def __iter__(self):
            inside.set()
            assert release.wait(timeout=5), "turn never reached send()'s read loop"
            return iter(self._lines)

        def close(self) -> None:
            pass

    class GatedPopen(FakePopen):
        def __init__(self, cmd, stdout_text: str = "", returncode: int = 0):
            super().__init__(cmd, stdout_text, returncode)
            cmds.append(self.cmd)
            self.stdout = GatedStdout(stdout_text)

    monkeypatch.setattr(
        pi_agent.subprocess,
        "Popen",
        lambda cmd, **kwargs: GatedPopen(cmd, _answer("noch mit dem alten Modell")),
    )

    conv = pi_agent.PiAgentConversation(model=SMART_ROUTER)
    replies: list[str] = []
    turn = threading.Thread(target=lambda: replies.append(conv.send("langsam")))
    turn.start()
    assert inside.wait(timeout=2), "send() did not start reading"

    conv.set_model(COPILOT_SONNET)
    release.set()
    turn.join(timeout=5)

    assert _flag(cmds[0], "--model") == SMART_ROUTER
    assert replies == ["noch mit dem alten Modell"]


# -- Where the picker's choices come from ----------------------------------


@pytest.fixture
def no_model_cache(monkeypatch):
    """list_pi_models() caches module-globally (it shells out to `pi`, ~2s), so
    without this each test would inherit the previous one's list -- and this
    machine's real E.ON/Copilot models."""
    monkeypatch.setattr(pi_agent, "_models_cache", (0.0, []))


def _fake_run(monkeypatch, table: str, calls: list[int] | None = None):
    def fake_run(cmd, **kwargs):
        if calls is not None:
            calls.append(1)
        assert cmd == [pi_agent.PI_BIN, "--offline", "--list-models"]
        return subprocess.CompletedProcess(cmd, 0, stdout=table, stderr="")

    monkeypatch.setattr(pi_agent.subprocess, "run", fake_run)


def test_list_pi_models_gives_value_and_label(no_model_cache, monkeypatch) -> None:
    _fake_run(monkeypatch, TABLE)
    monkeypatch.setattr(pi_agent, "_custom_model_names", lambda: {})

    models = pi_agent.list_pi_models()

    assert [value for value, _ in models] == [
        "eon-ai/qwen-3.8-flash",
        SMART_ROUTER,
        COPILOT_SONNET,
    ]
    assert dict(models)[COPILOT_SONNET] == "claude-sonnet-5 (github-copilot)"


def test_list_pi_models_prefers_a_declared_display_name(no_model_cache, monkeypatch) -> None:
    """models.json's `name` ("Smart Router (E.ON)") beats the raw id, since
    that's the name the user chose for the model in pi itself."""
    _fake_run(monkeypatch, TABLE)
    monkeypatch.setattr(
        pi_agent, "_custom_model_names", lambda: {SMART_ROUTER: "Smart Router (E.ON)"}
    )

    assert dict(pi_agent.list_pi_models())[SMART_ROUTER] == "Smart Router (E.ON)"


def test_list_pi_models_caches_until_forced_to_refetch(no_model_cache, monkeypatch) -> None:
    calls: list[int] = []
    _fake_run(monkeypatch, TABLE, calls)

    pi_agent.list_pi_models()
    pi_agent.list_pi_models()
    pi_agent.list_pi_models(force_refresh=True)

    assert len(calls) == 2, "repeat calls must reuse the cache, force_refresh must not"


def test_list_pi_models_never_raises(no_model_cache, monkeypatch) -> None:
    """`pi` missing must degrade to a picker offering free text, not take the
    whole cockpit down with it."""

    def boom(cmd, **kwargs):
        raise OSError("pi: command not found")

    monkeypatch.setattr(pi_agent.subprocess, "run", boom)

    assert pi_agent.list_pi_models() == []


def test_list_pi_models_keeps_the_last_good_list(no_model_cache, monkeypatch) -> None:
    _fake_run(monkeypatch, TABLE)
    good = pi_agent.list_pi_models()

    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, pi_agent.PI_MODELS_TIMEOUT_S)

    monkeypatch.setattr(pi_agent.subprocess, "run", boom)

    assert pi_agent.list_pi_models(force_refresh=True) == good


# -- Cockpit wiring --------------------------------------------------------


class _AppStub:
    def __init__(self, model: str | None):
        self._model = model

    def get_model(self) -> str | None:
        return self._model


def _patch_one_model(monkeypatch) -> None:
    """A one-entry pi list, so the two tests below differ only in whether the
    model in use is on that list or not."""
    monkeypatch.setattr(
        pi_agent, "list_pi_models", lambda force=False: [(SMART_ROUTER, "Smart Router")]
    )


def test_cockpit_choices_are_label_value_and_include_the_current_model(monkeypatch) -> None:
    """Gradio wants (label, value) -- the reverse of list_pi_models()'s own
    order -- and the picker has to show what's really in use even when pi's
    list doesn't contain it (a --model override, or the list call failing)."""
    _patch_one_model(monkeypatch)

    choices = cockpit._model_choices(_AppStub("github-copilot/gpt-5.5"))

    assert ("Smart Router", SMART_ROUTER) in choices
    assert choices[0] == ("github-copilot/gpt-5.5 (aktuell)", "github-copilot/gpt-5.5")


def test_cockpit_does_not_duplicate_a_model_already_listed(monkeypatch) -> None:
    _patch_one_model(monkeypatch)

    choices = cockpit._model_choices(_AppStub(SMART_ROUTER))

    assert [value for _, value in choices] == [SMART_ROUTER]


# -- Regression guard on the fakes ----------------------------------------


def test_switchable_fake_is_still_a_plain_backend(harness: Harness) -> None:
    """The fakes gained set_model for the tests above; this checks the fake is
    still a drop-in AgentConversation so every other suite's harness -- and
    App's normal turn path -- is unaffected."""
    harness.app.submit_text("ganz normal")

    assert wait_until(lambda: harness.llm.sent == ["ganz normal"])
    assert isinstance(harness.llm, FakeAgentConversation)
