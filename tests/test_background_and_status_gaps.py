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


def test_asking_for_status_mid_processing_is_queued_and_both_get_answered(harness: Harness) -> None:
    # 2026-10-0x update (see docs/specs/background-channel.md section 1,
    # finding 2): barge-in/steering was reworked so a second text input no
    # longer cancels the turn in flight -- it's queued like any other
    # steering message (same mechanism test_core_dialog_loop.py's
    # test_text_input_during_thinking_queues_steering_message pins). A
    # status question is indistinguishable from a new question to the code,
    # so it now rides along in the same queue instead of discarding the
    # original task -- the "no abort, no real peek either" gap this test
    # documents just moved from "the task is lost" to "the task finishes
    # late and nothing marks what it was answering"; see the spec for the
    # remaining open question (a real peek that doesn't touch the queue).
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
    # that, only submit_text()/on_toggle(). Today that just queues it.
    app.submit_text("wie weit bist du?")

    # Finding: the original task is no longer cancelled or discarded.
    assert task.cancel_calls == 0
    assert app._state == "thinking"  # still the original turn, uninterrupted
    # Input Separation: both user messages are visible immediately, well
    # before either answer exists.
    assert [m["role"] for m in app.get_history()] == ["user", "user"]

    task.release()  # original task finishes...
    assert wait_until(lambda: len(task.sent) == 2)  # ...and the status question starts
    task.release()
    assert wait_until(lambda: app._state == "idle")

    roles = [m["role"] for m in app.get_history()]
    contents = [m["content"] for m in app.get_history()]
    assert roles == ["user", "user", "assistant", "assistant"]
    assert contents[0] == "mach die lange aufgabe"
    assert contents[1] == "wie weit bist du?"
    # Both replies now surface, in the order the turns ran -- the original
    # task's result is no longer silently discarded...
    assert contents[2] == "Task-Ergebnis (zu spät)"
    assert contents[3] == "Keine Ahnung, bin neu hier"
    # ...but nothing in the history marks which question a given answer
    # belongs to, nor that "wie weit bist du?" never actually inspected the
    # first task's progress -- the same "no turn-membership concept" gap
    # noted in test_background_result_during_an_active_turn_does_not_corrupt_it
    # above, and the still-open "real peek" question from the spec.
