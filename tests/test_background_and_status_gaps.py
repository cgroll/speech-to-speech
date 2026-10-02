"""Pins down today's actual behaviour for the two gaps noted in
docs/specs/core-dialog-loop.md section 7 ("Hintergrund-Agenten / mehr als
eine Antwort pro Turn") -- written *before* deciding how (or whether) to
extend the state model, so any redesign has a documented baseline to
compare against instead of guessing.

These are deliberately not "does the spec hold" tests like
test_core_dialog_loop.py's -- the spec doesn't cover this yet. They're
"what does the code currently do" probes.
"""

from __future__ import annotations

from .conftest import Harness, wait_until
from .fakes import FakeAgentConversation

# -- Gap 1: a background result arriving outside any send() call -----------
#
# AgentConversation.send() is the only way App currently learns anything
# from a backend -- synchronous, one call in, one reply out. There is no
# callback/push path for "the agent has more to say, unprompted" (unlike
# on_thinking/on_image, which only fire *during* a send() call). The only
# existing prior art in the project, the `reminder` skill's Telegram
# messages, bypasses App entirely -- it's a separate Cloud Task calling the
# Telegram API directly, never touching App._history or App._llm. So there
# is currently no sanctioned way to inject a background result into the
# Cockpit/App path at all.
#
# These two tests probe the only thing structurally possible today: calling
# the private _append_history() directly, as a stand-in for "whatever a
# future background-delivery mechanism would have to call". That it *can*
# be called without going through _state_lock is itself the finding: history
# and state are independently locked, so nothing stops a message from
# appearing with no corresponding state transition at all.


def test_background_result_while_idle_appears_as_text_only_no_tts(harness: Harness) -> None:
    app = harness.app
    assert app._state == "idle"

    # Stand-in for "a background agent's result arrives" -- no public API
    # exists for this today, see module docstring.
    app._append_history("assistant", "Hintergrund-Ergebnis: Build fertig.")

    # State is untouched -- nothing about this went through Processing or
    # Responding, so get_state() would still show "Bereit" to the user.
    assert app._state == "idle"
    assert [m["role"] for m in app.get_history()] == ["assistant"]
    # And critically: nobody called _speak() for it. In voice mode this
    # message would be silent today -- shown in the cockpit's text pane
    # (if something polls get_history()), never read aloud, never
    # triggering an "Antwort beginnt" style event at all.
    assert harness.tts.speak_calls == []


def test_background_result_during_an_active_turn_does_not_corrupt_it(harness: Harness) -> None:
    app = harness.app
    app._llm = FakeAgentConversation(reply="normale Antwort", gate=True)

    app.submit_text("normale frage")
    assert wait_until(lambda: app._llm.started.is_set())
    assert app._state == "thinking"

    # A background result lands mid-turn, from an unrelated source.
    app._append_history("assistant", "Hintergrund-Ergebnis: fertig.")

    # The in-flight turn is unaffected -- state didn't move, _state_lock was
    # never touched by the background write.
    assert app._state == "thinking"
    roles_mid_turn = [m["role"] for m in app.get_history()]
    assert roles_mid_turn == ["user", "assistant"]
    # The finding worth flagging: the background entry is indistinguishable
    # from a real assistant reply to "normale frage" -- it's inserted
    # *before* the actual answer even exists yet. A chat UI rendering this
    # list in order would show the background message as if it already
    # answered the still-pending question.

    app._llm.release()
    assert wait_until(lambda: app._state == "idle")
    final_roles = [m["role"] for m in app.get_history()]
    assert final_roles == ["user", "assistant", "assistant"]
    assert app.get_history()[-1]["content"] == "normale Antwort"
    # The real answer lands *after* the background message in the list,
    # even though it was asked for *before* it -- ordering follows arrival
    # time, not logical turn membership, because there's no concept of
    # "which turn does this message belong to" at all.


# -- Gap 2: "what's the status?" during Processing --------------------------
#
# There is exactly one way today to send new input while a turn is already
# running: submit_text()/on_toggle() during "thinking"/"speaking", which the
# spec defines as Barge-in -- Stop, then the new input starts a fresh turn.
# A status question isn't trying to replace the running task, just peek at
# it, but the code has no way to tell the two apart: both are "new input
# while not idle".


def test_asking_for_status_mid_processing_is_barge_in_and_discards_the_task(harness: Harness) -> None:
    app = harness.app
    replies = {
        "mach die lange aufgabe": "Task-Ergebnis (zu spät)",
        "wie weit bist du?": "Keine Ahnung, bin neu hier",
    }
    task = FakeAgentConversation(reply=lambda text: replies[text], gate=True)
    app._llm = task

    app.submit_text("mach die lange aufgabe")
    assert wait_until(lambda: task.started.is_set())
    assert app._state == "thinking"

    # The user just wants to peek, not cancel -- but there's no API for
    # that, only submit_text()/on_toggle(), which both barge in.
    app.submit_text("wie weit bist du?")

    # Finding: the original task is cancelled, not paused/queried.
    assert task.cancel_calls == 1
    assert app._state == "thinking"  # new turn, not idle -- looks seamless
    assert app._interrupt is not None

    # The original task's eventual reply, once the gate releases, is
    # discarded as an interrupted turn -- "Task-Ergebnis" never surfaces
    # anywhere, silently. From the user's perspective they asked a
    # clarifying question and the original request just vanished.
    #
    # The barge-in's own cancel() already released the *first* call's gate
    # (before the second thread even started, see submit_text()); wait for
    # the second call to actually be inside send() -- i.e. its own fresh
    # gate is in place -- before releasing it too, rather than racing it.
    assert wait_until(lambda: len(task.sent) == 2)
    task.release()
    assert wait_until(lambda: app._state == "idle")
    roles = [m["role"] for m in app.get_history()]
    contents = [m["content"] for m in app.get_history()]
    assert "Task-Ergebnis (zu spät)" not in contents
    assert roles == ["user", "user", "assistant"]
    assert contents[0] == "mach die lange aufgabe"
    assert contents[1] == "wie weit bist du?"
    # The second user message got its own, unrelated answer (from the same
    # fake backend/task object, since App reuses one backend across turns);
    # nothing in the history marks that the first request was ever
    # abandoned, the same "no abort marker today" gap
    # thinking-channel-and-stop-marker.md already names for plain Stop.
