"""The state machine, driven by recorded screens and canned model answers.

Every stop reason in the design gets its own case.
"""

from __future__ import annotations

import pytest

import labels as L
from conftest import FakeAdapter, FakeClient, Turn, build, fixture_text

from jev_hands.core import models as M
from jev_hands.core.loop import Engine
from jev_hands.errors import JevHandsError as JevHandsErr
from jev_hands.core.policy import (
    FLAG_CONTRADICTION,
    FLAG_MODEL_DRIFT,
    FLAG_UNVERIFIED_EXECUTE,
    Policy,
    Thresholds,
)

HOME = "delivery_home.xml"
SEARCH = "delivery_search.xml"
FOOD_LIST = "delivery_food_list.xml"
CHAT_APP = "chat_empty_tree.xml"
PROFILE = "delivery_profile.xml"
CHOOSER = "popup_app_clone_chooser.xml"
RESULT = "u2_delivery_search_result.xml"
# The delivery app results page one frame early, as round 8 recorded it twice: a
# back button and the query text, and nothing else in the tree yet.
HALF_DRAWN = "delivery_search_result_half_drawn.xml"


def fast_policy(**kwargs) -> Policy:
    """No waiting and no second settle read: one observation per settle."""
    kwargs.setdefault("settle_seconds", 0.0)
    kwargs.setdefault("settle_confirm_seconds", 0.0)
    return Policy(**kwargs)


def steady_policy(**kwargs) -> Policy:
    """Both second reads switched on, with the waits themselves down to nothing.

    What is being tested is whether two reads in a row agree, not how long the
    loop sleeps between them.
    """
    kwargs.setdefault("settle_min_seconds", 0.0)
    kwargs.setdefault("settle_extra_seconds", 0.001)
    kwargs.setdefault("settle_confirm_seconds", 0.001)
    return Policy(**kwargs)


def engine(screens, turns, *, policy=None, **adapter_kwargs) -> Engine:
    adapter = FakeAdapter([fixture_text(name) for name in screens], **adapter_kwargs)
    return Engine(adapter, FakeClient(turns), policy or fast_policy())


def index_of(name_fragment: str, fixture: str) -> str:
    screen = build(fixture)
    for element in screen.elements:
        if name_fragment in element.name:
            return str(element.index)
    raise AssertionError(f"no element containing {name_fragment!r} in {fixture}")


# --- the happy path -----------------------------------------------------------


def test_a_confident_step_executes_and_reports_the_change():
    target = index_of(L.DELIVERY, HOME)
    eng = engine([HOME, FOOD_LIST], [Turn(tap=target, tap_confidence=0.96)])
    step = eng.step(goal="open the delivery channel")
    assert step.stop_reason is None
    assert step.executed is True
    assert step.changed is True
    assert step.decision.tier == M.TIER_ACT
    assert step.decision.consumed is True
    assert eng.adapter.executed[0]["type"] == M.ACTION_TAP
    assert "x" in eng.adapter.executed[0] and "y" in eng.adapter.executed[0]


def test_the_decision_is_marked_consumed_before_the_action_runs():
    target = index_of(L.DELIVERY, HOME)
    eng = engine([HOME], [Turn(tap=target)], execute_error=True)
    step = eng.step(goal="open the delivery channel")
    assert step.decision.consumed is True
    assert step.executed is False
    assert step.stop_reason == M.STOP_EXECUTE_UNCERTAIN


def test_recent_actions_carry_into_the_next_request():
    target = index_of(L.DELIVERY, HOME)
    eng = engine([HOME, FOOD_LIST, FOOD_LIST], [Turn(tap=target)])
    eng.step(goal="open the delivery channel")
    eng.step(goal="open the delivery channel")
    second_state = eng.client.calls[1]["state"]
    assert second_state["recent_actions"]
    assert "screen changed" in second_state["recent_actions"][0]


# --- one case per stop reason -------------------------------------------------


def test_stop_reason_reached():
    eng = engine([HOME, HOME], [Turn(tap="0", tap_confidence=0.7, action_confidence=0.7, reached=0.9)])
    step = eng.step(goal="get to the home page", end_state="the home page is on screen")
    assert step.stop_reason == M.STOP_REACHED
    assert step.executed is False
    assert step.reached_content == pytest.approx(0.9)
    assert step.reached_entry == pytest.approx(0.05)
    assert step.reached_confirmed is True


def test_stop_reason_ask_low_confidence():
    eng = engine([HOME], [Turn(tap="3", tap_confidence=0.55, action_confidence=0.9)])
    step = eng.step(goal="search for something")
    assert step.stop_reason == M.STOP_ASK_LOW_CONFIDENCE
    assert step.decision.tier == M.TIER_ASK
    assert step.executed is False
    assert len(step.decision.top3) == 3


def test_stop_reason_ask_contradiction():
    eng = engine([HOME], [Turn(action="tap_element", tap="none", tap_confidence=0.9)])
    step = eng.step(goal="do something impossible")
    assert step.stop_reason == M.STOP_ASK_CONTRADICTION
    assert FLAG_CONTRADICTION in step.decision.flags
    assert step.executed is False


def test_stop_reason_ask_popup():
    eng = engine([HOME], [Turn(tap="0", popup=0.66)])
    step = eng.step(goal="open something")
    assert step.stop_reason == M.STOP_ASK_POPUP
    assert step.executed is False


def test_stop_reason_stop_none_when_the_model_says_none():
    eng = engine([HOME], [Turn(action="none", action_confidence=0.7, tap="none")])
    step = eng.step(goal="check yesterday's step count")
    assert step.stop_reason == M.STOP_NONE
    assert step.executed is False


def test_stop_reason_stop_none_when_confidence_is_in_the_bottom_tier():
    eng = engine([HOME], [Turn(tap="3", tap_confidence=0.30, action_confidence=0.9)])
    step = eng.step(goal="something vague")
    assert step.decision.tier == M.TIER_STOP
    assert step.stop_reason == M.STOP_NONE


def test_a_consequential_looking_element_is_not_held_back():
    """No element name holds a step back: whatever passes the confidence tier
    executes. The plan marks the boundaries, the phone asks for the
    fingerprint."""
    target = index_of(L.DELIVERY, HOME)
    eng = engine([HOME, FOOD_LIST], [Turn(tap=target, tap_confidence=0.99)])
    step = eng.step(goal="place the order")
    assert step.executed is True
    assert step.decision.flags == []


def test_stop_reason_invalid_answer_after_one_retry():
    eng = engine([HOME, HOME, HOME], [Turn(tap="3", broken="sum")])
    step = eng.step(goal="search for something")
    assert step.stop_reason == M.STOP_INVALID_ANSWER
    assert step.executed is False
    assert len(eng.client.calls) == 2  # asked once more after re-observing


def test_an_invalid_answer_that_comes_good_on_the_retry_proceeds():
    eng = engine(
        [HOME, HOME, FOOD_LIST],
        [Turn(tap="3", broken="keys"), Turn(tap=index_of(L.DELIVERY, HOME))],
    )
    step = eng.step(goal="open the delivery channel")
    assert step.stop_reason is None
    assert step.executed is True


def test_stop_reason_unchanged_twice():
    target = index_of(L.DELIVERY, HOME)
    eng = engine([HOME], [Turn(tap=target)])
    first = eng.step(goal="open the delivery channel")
    assert first.changed is False
    assert first.stop_reason is None
    second = eng.step(goal="open the delivery channel")
    assert second.changed is False
    assert second.stop_reason == M.STOP_UNCHANGED_TWICE


def test_stop_reason_observe_failed_when_the_app_hides_its_tree():
    eng = engine([CHAT_APP], [Turn(tap="0")])
    step = eng.step(goal="open a chat")
    assert step.stop_reason == M.STOP_OBSERVE_FAILED
    assert step.error["reason"] == M.OBSERVE_HIDDEN_TREE
    hint = step.error["hint"].lower()
    assert "secure window" in hint and "black" in hint
    assert eng.client.calls == [], "nothing to ask the model about"


def test_stop_reason_observe_failed_when_the_dump_itself_fails():
    eng = engine([HOME], [Turn(tap="0")], observe_errors=[True])
    step = eng.step(goal="open something")
    assert step.stop_reason == M.STOP_OBSERVE_FAILED
    assert step.error["error"] == "observe_failed"
    assert step.error["reason"] == M.OBSERVE_DUMP_ERROR


def test_stop_reason_execute_uncertain_never_retries_the_write():
    target = index_of(L.DELIVERY, HOME)
    eng = engine([HOME], [Turn(tap=target)], execute_error=True)
    step = eng.step(goal="open the delivery channel")
    assert step.stop_reason == M.STOP_EXECUTE_UNCERTAIN
    assert eng.adapter.executed == []
    assert step.error["error"] == "execute_uncertain"


# --- the reached gate and the two confirmations behind it ---------------------

END_STATE = "an editable search box with the cursor in it is at the top of the screen"


def test_a_gate_the_confirmation_backs_up_stops_the_run():
    eng = engine(
        [HOME, HOME],
        [
            Turn(
                tap="0",
                tap_confidence=0.7,
                action_confidence=0.7,
                reached=0.4,
                content=0.88,
                entry=0.12,
            )
        ],
    )
    step = eng.step(goal="open the search page", end_state=END_STATE)
    assert step.stop_reason == M.STOP_REACHED
    assert step.executed is False
    assert step.reached_candidate is True
    assert step.reached_content == pytest.approx(0.88)
    assert step.reached_entry == pytest.approx(0.12)
    assert step.reached_confirmed is True
    # The field stays, at zero: the confirmations no longer have a request of
    # their own to spend time in, and a harness summing the timings keeps
    # working.
    assert step.timings["confirm_ms"] == 0
    assert step.to_json()["stop_hint"] == M.STOP_REASON_HINTS[M.STOP_REACHED]


def test_a_gate_the_confirmations_do_not_back_up_carries_on_and_acts():
    """Round 5, three runs out of ten: `reached` read 0.25 to 0.29 on a home
    page whose search bar only opened the search page, and the run stopped
    there. Batch G reads 0.39 to 0.46 for content and 0.81 to 0.90 for entry on
    those same screens, so the entry question is what holds the step back and
    it goes ahead and taps."""
    target = index_of(L.DELIVERY, HOME)
    eng = engine([HOME, FOOD_LIST], [Turn(tap=target, reached=0.29, content=0.46, entry=0.82)])
    step = eng.step(goal="open the search page", end_state=END_STATE)
    assert step.stop_reason is None
    assert step.executed is True
    assert step.reached_candidate is True
    assert step.reached_content == pytest.approx(0.46)
    assert step.reached_entry == pytest.approx(0.82)
    assert step.reached_confirmed is False
    assert eng.adapter.executed[0]["type"] == M.ACTION_TAP


def test_the_entry_question_alone_can_hold_a_step_back():
    """The content reading on a delivery app home page asked for a search box is as
    high as 0.46 - over the content bar. Only `reached_entry` tells it apart
    from the search page, which is the whole point of splitting the question."""
    target = index_of(L.DELIVERY, HOME)
    eng = engine([HOME, FOOD_LIST], [Turn(tap=target, reached=0.6, content=0.9, entry=0.82)])
    step = eng.step(goal="open the search page", end_state=END_STATE)
    assert step.reached_candidate is True
    assert step.reached_content == pytest.approx(0.9)
    assert step.reached_confirmed is False, "the entry question is the only one saying no"
    assert step.executed is True


def test_a_low_content_no_longer_refuses_a_destination():
    """Round 8, three gate hits out of 24: the step stood on the destination,
    `reached_entry` read 0.12 to 0.38 and said so, and `reached_content` read
    0.30 to 0.38 and refused it - two of the three on a results page that had
    not finished drawing. The confirmation is `reached` and `reached_entry`;
    content opens the gate and says nothing about stopping. The numbers here
    are round 8's T1a_run1 step 11."""
    eng = engine(
        [HOME],
        [Turn(action="none", action_confidence=0.7, tap="none",
              reached=0.51, content=0.30, entry=0.17)],
    )
    step = eng.step(goal="open the search page", end_state=END_STATE)
    assert step.stop_reason == M.STOP_REACHED
    assert step.reached_candidate is True
    assert step.reached_content == pytest.approx(0.30), "reported, not weighed"
    assert step.reached_confirmed is True
    assert step.to_json()["stop_hint"] == M.STOP_REASON_HINTS[M.STOP_REACHED]


def test_content_alone_still_cannot_confirm_a_screen():
    """The other half of taking content out of the verdict: it opens the gate
    on a destination whose `reached` reads low, and a step that gets in that
    way is a candidate to look at, never a confirmed arrival."""
    eng = engine(
        [HOME],
        [Turn(action="none", action_confidence=0.7, tap="none",
              reached=0.15, content=0.95, entry=0.08)],
    )
    step = eng.step(goal="open the search page", end_state=END_STATE)
    assert step.reached_candidate is True
    assert step.reached_confirmed is False


def test_a_contradiction_of_the_none_kind_reports_the_end_state_readings():
    """Round 7 lost T1b_run1 here: `action: none` with a strong tap target is
    what a finished screen answers, the contradiction is checked before the
    gate, and the step came back with no `end_state` readings on it at all.
    The stop reason is unchanged; the readings now ride along."""
    eng = engine(
        [HOME],
        [Turn(action="none", action_confidence=0.9, tap="0", tap_confidence=0.95,
              reached=0.15, content=0.9, entry=0.08)],
    )
    step = eng.step(goal="open the search page", end_state=END_STATE)
    assert step.stop_reason == M.STOP_ASK_CONTRADICTION
    assert step.executed is False
    assert step.contradiction_at_end_state is True
    assert step.contradiction_kind == "none_but_strong_target"
    assert step.reached_candidate is True, "the content question opened the gate"
    assert step.reached_content == pytest.approx(0.9)
    assert step.reached_entry == pytest.approx(0.08)
    assert step.reached_confirmed is False, "`reached` itself is still under its bar"
    payload = step.to_json()
    assert payload["contradiction_kind"] == "none_but_strong_target"
    hint = payload["stop_hint"]
    assert "reached 0.15, content 0.90, entry 0.08" in hint
    assert "jev_check" in hint


def test_a_contradiction_of_the_tap_kind_reports_them_too():
    """The kind batch G did not cover, and the only kind a phone produced:
    rounds 7 and 8 hit `action: tap_element` with `tap_target: none` four
    times out of four, twice on a screen that was the destination, and the
    readings never reached the caller. These numbers are round 8's T1b_run1.
    Any contradiction on a step with an `end_state` reports them now."""
    eng = engine(
        [HOME],
        [Turn(action="tap_element", tap="none", tap_confidence=0.9,
              reached=0.64, content=0.53, entry=0.12)],
    )
    step = eng.step(goal="open the search page", end_state=END_STATE)
    assert step.stop_reason == M.STOP_ASK_CONTRADICTION
    assert step.executed is False
    assert step.contradiction_at_end_state is True
    assert step.contradiction_kind == "tap_but_no_target"
    assert step.reached_candidate is True
    assert step.reached_content == pytest.approx(0.53)
    assert step.reached_entry == pytest.approx(0.12)
    assert step.reached_confirmed is True, "all three readings say destination"
    payload = step.to_json()
    assert payload["contradiction_kind"] == "tap_but_no_target"
    assert "reached 0.64, content 0.53, entry 0.12" in payload["stop_hint"]


def test_without_an_end_state_there_are_no_readings_but_still_a_kind():
    """Nothing was asked about arriving anywhere, so there is nothing to
    report; which contradiction fired is still worth saying."""
    eng = engine(
        [HOME],
        [Turn(action="none", action_confidence=0.9, tap="0", tap_confidence=0.95)],
    )
    step = eng.step(goal="open the search page")
    assert step.stop_reason == M.STOP_ASK_CONTRADICTION
    assert step.contradiction_at_end_state is False
    assert step.reached_candidate is False
    assert step.contradiction_kind == "none_but_strong_target"
    assert step.to_json()["stop_hint"] == M.STOP_REASON_HINTS[M.STOP_ASK_CONTRADICTION]


def test_a_low_reached_no_longer_hides_a_destination():
    """Round 7, T1b_run1 step 11: the destination read `reached` 0.15 and the
    step never got near the gate. The gate now opens on the content question
    too, so the step comes back as a `reached` candidate."""
    eng = engine(
        [HOME],
        [Turn(action="none", action_confidence=0.7, tap="none", reached=0.15,
              content=0.9, entry=0.08)],
    )
    step = eng.step(goal="open the search page", end_state=END_STATE)
    assert step.stop_reason == M.STOP_REACHED
    assert step.reached_candidate is True
    # `reached` itself is still under its bar, so this is a candidate the
    # caller has to look at, not a confirmed arrival.
    assert step.reached_confirmed is False
    assert step.to_json()["stop_hint"] == M.STOP_REACHED_UNCONFIRMED_HINT


def test_the_confirmations_cost_no_second_request_and_no_extra_tree_read():
    """Batch F put the confirmation in the request that was going out anyway;
    batch G puts two there. Round 6 sent one separately and paid 496 ms."""
    target = index_of(L.DELIVERY, HOME)
    eng = engine([HOME, FOOD_LIST], [Turn(tap=target, reached=0.29, content=0.24, entry=0.82)])
    before = eng.adapter.observations
    step = eng.step(goal="open the search page", end_state=END_STATE)
    assert eng.client.checks == [], "no request of their own"
    assert len(eng.client.calls) == 1
    # one read to start the step, and the one the settle pays for; neither
    # belongs to the confirmations.
    assert eng.adapter.observations == before + 2
    asked = eng.client.calls[0]["questions"]
    assert asked["reached_content"]["criteria"]
    assert asked["reached_entry"]["criteria"]
    assert step.decision.reached == pytest.approx(0.29)
    # All three readings come back on the decision, and the usage of the one
    # request already covers what the extra questions cost.
    assert step.decision.reached_content == pytest.approx(0.24)
    assert step.decision.reached_entry == pytest.approx(0.82)
    assert step.decision.to_json()["reached_content"] == pytest.approx(0.24)
    assert step.decision.to_json()["reached_entry"] == pytest.approx(0.82)
    assert step.decision.usage["input_tokens"] > 0


def test_the_confirmations_judge_the_screen_the_decision_was_made_on():
    """One request, one state, so the three readings cannot be about different
    screens. The end state reaches the questions through `state.end_state`."""
    eng = engine([HOME, HOME], [Turn(tap="0", tap_confidence=0.7, action_confidence=0.7, reached=0.9)])
    step = eng.step(goal="open the search page", end_state=END_STATE)
    request = eng.client.calls[0]
    assert request["state"]["end_state"] == END_STATE
    for qid in ("reached_content", "reached_entry"):
        assert "`end_state`" in request["questions"][qid]["instructions"]
    assert step.stop_reason == M.STOP_REACHED


def test_an_unconfirmed_gate_with_nothing_left_to_do_stops_as_reached():
    """Round 6, twice out of ten runs: the gate opened on the destination, the
    confirmation came in low, the model answered `none` because there was
    nothing worth tapping, and the run ended as `stop_none` - whose hint sends
    the caller off to re-plan. It stops on the screen it is on, and says the
    confirmations did not back it up."""
    eng = engine(
        [HOME],
        [Turn(action="none", action_confidence=0.7, tap="none", reached=0.9,
              content=0.10, entry=0.80)],
    )
    step = eng.step(goal="open the search page", end_state=END_STATE)
    assert step.stop_reason == M.STOP_REACHED
    assert step.executed is False
    assert step.reached_candidate is True
    assert step.reached_confirmed is False
    payload = step.to_json()
    assert payload["reached_confirmed"] is False
    assert payload["stop_hint"] == M.STOP_REACHED_UNCONFIRMED_HINT
    assert "jev_check" in payload["stop_hint"]


def test_an_unconfirmed_gate_still_answers_to_the_gates_behind_it():
    """Dropping the `reached` stop must not turn a step the confidence policy
    would have refused into a step that acts."""
    target = index_of(L.DELIVERY, HOME)
    eng = engine(
        [HOME],
        [Turn(tap=target, tap_confidence=0.3, action_confidence=0.3, reached=0.9,
              content=0.10, entry=0.80)],
    )
    step = eng.step(goal="open the search page", end_state=END_STATE)
    assert step.stop_reason == M.STOP_NONE
    assert step.executed is False
    assert step.reached_candidate is True
    assert step.reached_confirmed is False


def test_below_both_ways_into_the_gate_nothing_is_read():
    target = index_of(L.DELIVERY, HOME)
    eng = engine([HOME, FOOD_LIST], [Turn(tap=target, reached=0.2, content=0.2, entry=0.9)])
    step = eng.step(goal="open the search page", end_state=END_STATE)
    assert step.executed is True
    assert step.reached_candidate is False
    assert step.reached_content is None
    assert step.reached_entry is None
    assert eng.client.checks == []
    assert "confirm_ms" not in step.timings


def test_a_missing_confirmation_answer_still_stops_as_reached():
    """The confirmations are two questions among several and the response may
    come back without them. Being wrong by stopping costs a round trip; being
    wrong by acting does not come back."""
    eng = engine([HOME, HOME], [Turn(tap="0", tap_confidence=0.7, action_confidence=0.7, reached=0.9)])
    answered = eng.client.evaluate

    def without_the_confirmations(state, question_set, *, model="jev-latest"):
        response = answered(state, question_set, model=model)
        response.answers.pop("reached_content", None)
        response.answers.pop("reached_entry", None)
        return response

    eng.client.evaluate = without_the_confirmations
    step = eng.step(goal="open the search page", end_state=END_STATE)
    assert step.stop_reason == M.STOP_REACHED
    assert step.reached_candidate is True
    assert step.reached_content is None
    assert step.reached_entry is None
    assert step.reached_confirmed is None
    assert step.to_json()["stop_hint"] == M.STOP_REASON_HINTS[M.STOP_REACHED]


def test_a_run_keeps_going_through_an_unconfirmed_gate():
    target = index_of(L.DELIVERY, HOME)
    eng = engine(
        [HOME, FOOD_LIST, FOOD_LIST],
        [
            Turn(tap=target, reached=0.29, content=0.24, entry=0.82),
            Turn(action="none", action_confidence=0.8, tap="none", reached=0.95,
                 content=0.97, entry=0.05),
        ],
    )
    result = eng.run(goal="open the search page", end_state=END_STATE, max_steps=3)
    assert result.steps[0].executed is True
    assert result.steps[0].reached_candidate is True
    assert result.stop_reason == M.STOP_REACHED
    assert result.steps[-1].reached_content == pytest.approx(0.97)
    assert result.steps[-1].reached_confirmed is True


def test_a_run_counts_its_gate_hits_its_confirmations_and_its_unconfirmed_stops():
    """A measurement round reads these off the result instead of walking the
    steps, the same way it reads `stale_count` and `reused_count`."""
    target = index_of(L.DELIVERY, HOME)
    eng = engine(
        [HOME, FOOD_LIST, FOOD_LIST],
        [
            Turn(tap=target, reached=0.29, content=0.24, entry=0.82),
            Turn(action="none", action_confidence=0.8, tap="none", reached=0.95,
                 content=0.30, entry=0.70),
        ],
    )
    result = eng.run(goal="open the search page", end_state=END_STATE, max_steps=3)
    assert result.stop_reason == M.STOP_REACHED
    assert result.gate_hits == 2
    assert result.confirmed == 0
    assert result.unconfirmed_stops == 1
    payload = result.to_json()
    assert payload["gate_hits"] == 2
    assert payload["confirmed"] == 0
    assert payload["unconfirmed_stops"] == 1
    assert payload["stop_hint"] == M.STOP_REACHED_UNCONFIRMED_HINT


# --- a screen that has not finished drawing ----------------------------------

RESULTS_END_STATE = "a list of shop results is on screen"


def _half_drawn_turns(second, third=None):
    """A tap, then the readings taken on the half-drawn frame, then the
    readings taken on the frame after it."""
    turns = [Turn(tap=index_of(L.SEARCH, SEARCH)), second]
    if third is not None:
        turns.append(third)
    return turns


def test_a_half_drawn_screen_is_read_again_before_it_ends_a_run():
    """Round 8 stopped twice on a results page whose tree held two rows. Both
    times `reached` opened the gate, `reached_content` read 0.30 and 0.35 on a
    screen whose content had not been drawn yet, and both screenshots showed a
    full list of shops. A table that small, one step after a table that big,
    buys one wait and one more look before anything is read off it."""
    eng = engine(
        [SEARCH, HALF_DRAWN, RESULT],
        _half_drawn_turns(
            Turn(action="none", action_confidence=0.8, tap="none",
                 reached=0.51, content=0.30, entry=0.17),
            Turn(action="none", action_confidence=0.8, tap="none",
                 reached=0.66, content=0.54, entry=0.10),
        ),
    )
    result = eng.run(goal="submit the search", end_state=RESULTS_END_STATE, max_steps=3)
    assert result.stop_reason == M.STOP_REACHED
    step = result.steps[1]
    assert step.rerendered is True
    assert step.to_json()["rerendered"] is True
    assert len(step.before.elements) == 30, "it decided on the screen that drew"
    assert step.reached_content == pytest.approx(0.54)
    assert step.reached_confirmed is True
    assert step.timings["reused_observation"] is False
    assert "rerender_ms" in step.timings
    assert eng.adapter.observations == 3
    assert len(eng.client.calls) == 3


def test_a_small_screen_after_a_small_screen_is_taken_as_it_is():
    """The rule is about a table collapsing, not about small screens. An app
    chooser is five rows and the screen after it is allowed to be small too."""
    eng = engine(
        [CHOOSER, HALF_DRAWN],
        [
            Turn(tap="0"),
            Turn(action="none", action_confidence=0.8, tap="none",
                 reached=0.51, content=0.30, entry=0.17),
        ],
    )
    result = eng.run(goal="submit the search", end_state=RESULTS_END_STATE, max_steps=3)
    assert result.stop_reason == M.STOP_REACHED
    assert result.steps[1].rerendered is False
    assert "rerender_ms" not in result.steps[1].timings
    assert eng.adapter.observations == 2
    assert len(eng.client.calls) == 2


def test_a_half_drawn_screen_nobody_would_stop_on_is_left_alone():
    """No reading is being trusted here, so there is nothing to protect and no
    reason to spend a wait: the step goes through the gates as it always did."""
    eng = engine(
        [SEARCH, HALF_DRAWN],
        _half_drawn_turns(
            Turn(action="none", action_confidence=0.8, tap="none",
                 reached=0.05, content=0.10, entry=0.90),
        ),
    )
    result = eng.run(goal="submit the search", end_state=RESULTS_END_STATE, max_steps=3)
    assert result.stop_reason == M.STOP_NONE
    assert result.steps[1].rerendered is False
    assert eng.adapter.observations == 2


def test_the_second_look_is_the_last_one():
    """A page that is still half drawn on the second look is decided on all
    the same. One extra read per step, never a loop."""
    eng = engine(
        [SEARCH, HALF_DRAWN, HALF_DRAWN],
        _half_drawn_turns(
            Turn(action="none", action_confidence=0.8, tap="none",
                 reached=0.51, content=0.30, entry=0.17),
            Turn(action="none", action_confidence=0.8, tap="none",
                 reached=0.51, content=0.30, entry=0.17),
        ),
    )
    result = eng.run(goal="submit the search", end_state=RESULTS_END_STATE, max_steps=3)
    assert result.stop_reason == M.STOP_REACHED
    step = result.steps[1]
    assert step.rerendered is True
    assert len(step.before.elements) == 2
    assert eng.adapter.observations == 3, "one look more, not a loop"
    assert len(eng.client.calls) == 3


def test_a_step_that_cannot_read_the_screen_again_keeps_what_it_had():
    """The second look is a nicety. When the tree cannot be read at all the
    step still has its first decision, and the freshness check before the tap
    is what protects the phone from acting on an old screen."""
    eng = engine(
        [SEARCH, HALF_DRAWN, RESULT],
        _half_drawn_turns(
            Turn(action="none", action_confidence=0.8, tap="none",
                 reached=0.51, content=0.30, entry=0.17),
        ),
        observe_errors=[False, False, True],
    )
    result = eng.run(goal="submit the search", end_state=RESULTS_END_STATE, max_steps=3)
    assert result.stop_reason == M.STOP_REACHED
    step = result.steps[1]
    assert step.rerendered is False
    assert len(step.before.elements) == 2
    assert len(eng.client.calls) == 2


# --- looking again before acting ---------------------------------------------


def test_the_screen_is_read_again_immediately_before_acting():
    target = index_of(L.DELIVERY, HOME)
    eng = engine([HOME, FOOD_LIST], [Turn(tap=target)])
    step = eng.step(goal="open the delivery channel")
    assert eng.adapter.freshness_checks == 1
    assert step.executed is True
    assert "verify_ms" in step.timings


def test_stop_reason_stale_screen_when_the_page_moved_under_us():
    target = index_of(L.DELIVERY, HOME)
    eng = engine([HOME, FOOD_LIST], [Turn(tap=target)], stale_after=0)
    step = eng.step(goal="open the delivery channel")
    assert step.stop_reason == M.STOP_STALE_SCREEN
    assert step.executed is False
    assert eng.adapter.executed == []
    # Still consumed: a decision made on a screen that no longer exists must
    # never be replayed later.
    assert step.decision.consumed is True


def test_run_keeps_going_after_a_stale_screen_and_costs_one_step():
    target = index_of(L.DELIVERY, HOME)
    eng = engine([HOME, HOME, FOOD_LIST, FOOD_LIST], [Turn(tap=target)], stale_after=1)
    result = eng.run(goal="open the delivery channel", max_steps=4)
    reasons = [s.stop_reason for s in result.steps]
    assert M.STOP_STALE_SCREEN in reasons
    assert len(result.steps) > reasons.index(M.STOP_STALE_SCREEN) + 1
    assert eng.adapter.executed, "the run must still act once the screen settles"


def test_turning_the_check_off_skips_it_and_flags_the_decision():
    target = index_of(L.DELIVERY, HOME)
    eng = engine(
        [HOME, FOOD_LIST],
        [Turn(tap=target)],
        policy=fast_policy(verify_before_act=False),
    )
    step = eng.step(goal="open the delivery channel")
    assert eng.adapter.freshness_checks == 0
    assert step.executed is True
    assert FLAG_UNVERIFIED_EXECUTE in step.decision.flags


def test_act_does_not_read_the_tree_twice():
    eng = engine([HOME], [Turn(tap="0")])
    screen = eng.observe()
    before = eng.adapter.freshness_checks
    eng.act(action=M.ACTION_TAP, capture_id=screen.capture_id, index=4)
    assert eng.adapter.freshness_checks == before + 1


# --- check 6 -------------------------------------------------------------------


def test_deciding_on_a_screen_the_engine_has_moved_past_fails_check_six():
    from jev_hands.core import validate as validate_mod

    eng = engine([HOME, FOOD_LIST], [Turn(tap="0")])
    stale = eng.observe()
    eng.observe()  # the engine has moved on; `stale` is no longer current
    decision = eng.decide(stale, goal="open something")
    codes = [f["check"] for f in decision.raw["validation_failures"]]
    assert validate_mod.CHECK_CAPTURE_ID in codes


def test_check_six_passes_on_the_screen_that_was_just_observed():
    eng = engine([HOME], [Turn(tap="0")])
    screen = eng.observe()
    decision = eng.decide(screen, goal="open something")
    assert decision.raw["validation_failures"] == []


# --- pictures when someone has to look ------------------------------------------


def test_a_blocking_popup_stop_brings_a_screenshot_by_default(tmp_path):
    adapter = FakeAdapter([fixture_text(HOME)])
    eng = Engine(adapter, FakeClient([Turn(tap="0", popup=0.8)]), fast_policy(),
                 screenshot_dir=tmp_path)
    step = eng.step(goal="open something")
    assert step.stop_reason == M.STOP_ASK_POPUP
    assert step.screenshot_path
    assert step.to_json()["screenshot_path"] == step.screenshot_path


def test_an_unreadable_tree_brings_a_screenshot_by_default(tmp_path):
    adapter = FakeAdapter([fixture_text(CHAT_APP)])
    eng = Engine(adapter, FakeClient([Turn(tap="0")]), fast_policy(), screenshot_dir=tmp_path)
    step = eng.step(goal="open a chat")
    assert step.stop_reason == M.STOP_OBSERVE_FAILED
    assert step.screenshot_path


def test_a_low_confidence_stop_brings_one_only_when_asked(tmp_path):
    def build_engine():
        adapter = FakeAdapter([fixture_text(HOME)])
        return Engine(
            adapter,
            FakeClient([Turn(tap="3", tap_confidence=0.55, action_confidence=0.9)]),
            fast_policy(),
            screenshot_dir=tmp_path,
        )

    quiet = build_engine().step(goal="search for something")
    assert quiet.stop_reason == M.STOP_ASK_LOW_CONFIDENCE
    assert quiet.screenshot_path is None

    loud = build_engine().step(goal="search for something", screenshot_on_stop=True)
    assert loud.screenshot_path


def test_screenshots_can_be_turned_off_entirely(tmp_path):
    adapter = FakeAdapter([fixture_text(HOME)])
    eng = Engine(adapter, FakeClient([Turn(tap="0", popup=0.8)]), fast_policy(),
                 screenshot_dir=tmp_path)
    step = eng.step(goal="open something", screenshot_on_stop=False)
    assert step.stop_reason == M.STOP_ASK_POPUP
    assert step.screenshot_path is None
    assert adapter.screenshots == 0


def test_stop_reason_max_steps():
    target = index_of(L.DELIVERY, HOME)
    eng = engine([HOME, FOOD_LIST, HOME, FOOD_LIST, HOME, FOOD_LIST, HOME], [Turn(tap=target)])
    result = eng.run(goal="keep going", max_steps=3)
    assert result.stop_reason == M.STOP_MAX_STEPS
    assert len(result.steps) == 3


def test_every_stop_reason_is_covered_by_this_module():
    """Guard against a new stop reason arriving with no test behind it."""
    source = open(__file__, encoding="utf-8").read()
    for reason in M.STOP_REASONS:
        constant = next(
            name
            for name in dir(M)
            if name.startswith("STOP_") and getattr(M, name) == reason
        )
        assert f"M.{constant}" in source, reason
        assert M.STOP_REASON_HINTS[reason]


# --- run ----------------------------------------------------------------------


def test_a_run_counts_its_refused_and_its_reused_steps():
    """A measurement round reads these two off the result rather than walking
    every step of every run."""
    target = index_of(L.DELIVERY, HOME)
    refused = engine([HOME] * 8, [Turn(tap=target)], stale_after=0)
    result = refused.run(goal="open the delivery channel", max_steps=3)
    assert result.stale_count == 3
    assert result.reused_count == 0
    assert result.to_json()["stale_count"] == 3

    reusing = engine([HOME, FOOD_LIST, FOOD_LIST], [Turn(tap="0")], policy=steady_policy())
    reused = reusing.run(goal="keep going", max_steps=3)
    assert reused.reused_count >= 1
    payload = reused.to_json()
    assert payload["reused_count"] == reused.reused_count
    assert payload["stale_count"] == 0


def test_run_stops_at_the_first_stop_reason_and_keeps_the_trace():
    target = index_of(L.DELIVERY, HOME)
    eng = engine(
        [HOME, FOOD_LIST, FOOD_LIST],
        [Turn(tap=target), Turn(action="none", action_confidence=0.8, tap="none")],
    )
    result = eng.run(goal="open the delivery channel", max_steps=5)
    assert result.stop_reason == M.STOP_NONE
    assert len(result.steps) == 2
    assert result.steps[0].executed is True
    assert result.final is not None
    payload = result.to_json()
    assert payload["stop_hint"]


# --- scroll bookkeeping --------------------------------------------------------


def test_scroll_up_is_not_offered_before_anything_has_scrolled_down():
    eng = engine([PROFILE], [Turn(tap="0")])
    assert eng.scroll_hint() == {"can_down": True, "can_up": False}
    eng.step(goal="look around")
    offered = set(eng.client.calls[0]["questions"]["action"]["criteria"])
    assert M.ACTION_SCROLL_DOWN in offered
    assert M.ACTION_SCROLL_UP not in offered


def test_a_scroll_that_changes_nothing_is_withdrawn_next_time():
    eng = engine([PROFILE], [Turn(action="scroll_down", action_confidence=0.9, tap="none")])
    eng.step(goal="find the orders section")
    assert eng.scroll_hint()["can_down"] is False


def test_reaching_the_bottom_does_not_block_scrolling_down_forever():
    """down works, down hits the bottom, up works: down has to come back.
    Otherwise a run can never look further down the page again."""
    eng = engine(
        [PROFILE, FOOD_LIST, FOOD_LIST, FOOD_LIST, HOME, SEARCH],
        [Turn(action="scroll_down", action_confidence=0.9, tap="none")],
    )
    eng.step(goal="find the orders section")            # down, screen changed
    assert eng.scroll_hint() == {"can_down": True, "can_up": True}

    eng.step(goal="find the orders section")            # down again, nothing moved
    assert eng.scroll_hint()["can_down"] is False

    eng.client.turns = [Turn(action="scroll_up", action_confidence=0.9, tap="none")]
    eng.step(goal="go back up")                          # up, screen changed
    assert eng.scroll_hint()["can_down"] is True, "scroll_down must be offered again"
    assert eng.scroll_hint()["can_up"] is True


def test_a_scroll_that_works_unlocks_scrolling_back_up():
    eng = engine(
        [PROFILE, FOOD_LIST, FOOD_LIST],
        [Turn(action="scroll_down", action_confidence=0.9, tap="none")],
    )
    eng.step(goal="find the orders section")
    assert eng.scroll_hint() == {"can_down": True, "can_up": True}


# --- typing ---------------------------------------------------------------------


def test_typing_targets_an_editable_element_and_carries_the_text():
    target = index_of(L.BURGER_SHOP, SEARCH)
    eng = engine(
        [SEARCH, SEARCH],
        [Turn(action="type_text", action_confidence=0.95, type_target=target, tap="none")],
    )
    step = eng.step(goal="type the shop name", text_to_type=L.MILK_TEA)
    assert step.decision.action == M.ACTION_TYPE
    assert step.decision.target == int(target)
    assert step.decision.text_to_type == L.MILK_TEA
    executed = eng.adapter.executed[0]
    assert executed["type"] == M.ACTION_TYPE
    assert executed["text"] == L.MILK_TEA


# --- flags ------------------------------------------------------------------------


def test_model_drift_is_flagged_without_blocking_the_step():
    target = index_of(L.DELIVERY, HOME)
    adapter = FakeAdapter([fixture_text(HOME), fixture_text(FOOD_LIST)])
    eng = Engine(adapter, FakeClient([Turn(tap=target)], model="jev-2.0.0"), fast_policy())
    step = eng.step(goal="open the delivery channel")
    assert FLAG_MODEL_DRIFT in step.decision.flags
    assert step.executed is True


def test_confidence_is_the_weaker_of_the_action_and_the_target():
    target = index_of(L.DELIVERY, HOME)
    eng = engine([HOME, FOOD_LIST], [Turn(tap=target, tap_confidence=0.70, action_confidence=0.99)])
    step = eng.step(goal="open the delivery channel")
    assert step.decision.confidence == pytest.approx(0.70)


# --- caller driven action ----------------------------------------------------------


def test_act_refuses_an_index_from_a_screen_that_moved_on():
    from jev_hands.errors import JevHandsError

    eng = engine([HOME, FOOD_LIST], [Turn(tap="0")])
    first = eng.observe()
    eng.observe()
    with pytest.raises(JevHandsErr) as caught:
        eng.act(action=M.ACTION_TAP, capture_id=first.capture_id, index=0)
    assert caught.value.code == "stale_capture_id"


def test_act_executes_what_the_caller_chose():
    eng = engine([HOME], [Turn(tap="0")])
    screen = eng.observe()
    step = eng.act(action=M.ACTION_TAP, capture_id=screen.capture_id, index=4)
    assert step.executed is True
    assert eng.adapter.executed[0]["type"] == M.ACTION_TAP


# --- thresholds from policy ---------------------------------------------------------


def test_a_tighter_policy_turns_an_act_into_an_ask():
    target = index_of(L.DELIVERY, HOME)
    eng = engine(
        [HOME, FOOD_LIST],
        [Turn(tap=target, tap_confidence=0.80)],
        policy=fast_policy(thresholds=Thresholds(act=0.9, ask=0.5)),
    )
    step = eng.step(goal="open the delivery channel")
    assert step.stop_reason == M.STOP_ASK_LOW_CONFIDENCE


def test_a_page_that_never_settles_is_reported_as_stale_not_max_steps():
    """Spending the whole budget on refused steps is not the same as running
    out of room to work: the caller has to know the page never settled."""
    target = index_of(L.DELIVERY, HOME)
    eng = engine([HOME] * 8, [Turn(tap=target)], stale_after=0)
    result = eng.run(goal="open the delivery channel", max_steps=3)
    assert [s.stop_reason for s in result.steps] == [M.STOP_STALE_SCREEN] * 3
    assert result.stop_reason == M.STOP_STALE_SCREEN
    assert eng.adapter.executed == []


def test_act_without_a_target_is_not_refused_on_a_moving_page():
    """go_back and HOME reuse no coordinate from the old observation. Refusing
    them on a page that animates would leave the caller stranded on it."""
    eng = engine([HOME, FOOD_LIST], [], stale_after=0)
    screen = eng.observe()
    step = eng.act(action=M.ACTION_BACK, capture_id=screen.capture_id)
    assert step.executed is True
    assert eng.adapter.executed == [{"type": M.ACTION_BACK}]


def test_act_on_an_element_is_still_refused_when_the_screen_moved():
    eng = engine([HOME, FOOD_LIST], [], stale_after=0)
    screen = eng.observe()
    with pytest.raises(JevHandsErr) as caught:
        eng.act(action=M.ACTION_TAP, capture_id=screen.capture_id,
                index=index_of(L.DELIVERY, HOME))
    assert caught.value.code == "stale_capture_id"
    assert eng.adapter.executed == []


# --- speed: one observation per step, and a settle that stops early ------------


def test_run_reuses_the_screen_it_just_read_instead_of_reading_it_again():
    """The screen read after an action is the same screen the next step would
    read again a moment later. One tree read is dropped per step; the freshness
    check before acting still happens, so nothing is taken on trust."""
    eng = engine([HOME, FOOD_LIST, SEARCH], [Turn(tap="0", tap_confidence=0.96)])
    result = eng.run(goal="keep going", max_steps=2)
    first, second = result.steps

    assert second.before.capture_id == first.after.capture_id
    assert first.timings["reused_observation"] is False
    assert second.timings["reused_observation"] is True
    assert eng.adapter.observations == 3, "two steps used to cost four tree reads"
    assert eng.adapter.freshness_checks == 2, "both steps still looked again before acting"


def test_a_screen_that_reads_the_same_twice_is_handed_to_the_next_step():
    """[changed, same]: the action landed and the page has stopped drawing."""
    eng = engine([HOME, FOOD_LIST, FOOD_LIST], [Turn(tap="0")], policy=steady_policy())
    result = eng.run(goal="keep going", max_steps=2)
    first, second = result.steps
    assert first.changed is True
    assert first.unstable is False
    assert second.timings["reused_observation"] is True
    assert second.before.capture_id == first.after.capture_id


def test_a_screen_still_being_drawn_is_not_handed_to_the_next_step():
    """[changed, changed]: two reads in a row disagree, so the newer one is
    kept for the result and the next step reads the tree itself. This is the
    seven `stale_screen` steps of round 5, every one of them on a screen that
    had been read 0.35 s after the action."""
    eng = engine([HOME, FOOD_LIST, SEARCH, SEARCH], [Turn(tap="0")], policy=steady_policy())
    result = eng.run(goal="keep going", max_steps=2)
    first, second = result.steps
    assert first.unstable is True
    assert first.after is not None
    assert second.timings["reused_observation"] is False
    assert second.before.capture_id != first.after.capture_id


def test_an_unchanged_screen_is_handed_on_when_the_second_read_agrees():
    """[same, same]: nothing moved and nothing is still moving."""
    eng = engine([HOME], [Turn(tap="0")], policy=steady_policy())
    result = eng.run(goal="keep going", max_steps=2)
    first, second = result.steps
    assert first.changed is False
    assert first.unstable is False
    assert second.timings["reused_observation"] is True
    assert second.stop_reason == M.STOP_UNCHANGED_TWICE


def test_reuse_after_a_steady_pair_is_not_refused_on_the_next_step():
    eng = engine([HOME, FOOD_LIST, FOOD_LIST], [Turn(tap="0")], policy=steady_policy())
    result = eng.run(goal="keep going", max_steps=2)
    assert M.STOP_STALE_SCREEN not in [s.stop_reason for s in result.steps]
    assert result.steps[1].timings["reused_observation"] is True
    assert result.steps[1].executed is True
    assert len(eng.adapter.executed) == 2


def test_a_reused_screen_is_still_refused_when_it_has_gone_stale():
    eng = engine([HOME, FOOD_LIST, FOOD_LIST, FOOD_LIST], [Turn(tap="0")], stale_after=1)
    result = eng.run(goal="keep going", max_steps=2)
    assert result.steps[1].timings["reused_observation"] is True
    assert result.steps[1].stop_reason == M.STOP_STALE_SCREEN
    assert len(eng.adapter.executed) == 1


def test_the_stability_read_is_off_by_default_and_the_screen_is_handed_on(monkeypatch):
    """Batch F reverts to the round-5 reuse. Round 6 measured the stability
    read on a phone: about 750 ms on 92% of the executed steps and 62% of the
    reuse refused, against the four or five seconds of `stale_screen` retries
    it saved over ten runs."""
    import time as time_mod

    naps = []
    monkeypatch.setattr(time_mod, "sleep", lambda seconds: naps.append(seconds))

    eng = engine([HOME, FOOD_LIST, SEARCH], [Turn(tap="0")], policy=Policy())
    result = eng.run(goal="keep going", max_steps=2)
    first, second = result.steps
    assert naps == [0.35, 0.35], "one wait per action, no stability wait"
    assert first.timings["settle_confirm_ms"] == 0
    assert first.unstable is False
    assert second.timings["reused_observation"] is True
    assert second.before.capture_id == first.after.capture_id


def test_which_second_wait_is_paid_depends_on_what_the_action_did(monkeypatch):
    import time as time_mod

    naps = []
    monkeypatch.setattr(time_mod, "sleep", lambda seconds: naps.append(seconds))

    moved = engine(
        [HOME, FOOD_LIST], [Turn(tap="0")], policy=Policy(settle_confirm_seconds=0.25)
    )
    step = moved.step(goal="open something")
    assert naps == [0.35, 0.25], "a screen that moved gets the short stability wait"
    assert moved.adapter.observations == 3
    assert step.timings["settle_ms"] >= 0
    assert step.timings["settle_confirm_ms"] >= 0
    assert "observe_after_ms" in step.timings

    naps.clear()
    stuck = engine([HOME], [Turn(tap="0")], policy=Policy())
    stuck.step(goal="open something")
    assert naps == [0.35, 0.45], "an unchanged screen gets the long second wait"
    assert stuck.adapter.observations == 3


def test_the_legacy_settle_seconds_still_drives_both_waits(monkeypatch):
    import time as time_mod

    naps = []
    monkeypatch.setattr(time_mod, "sleep", lambda seconds: naps.append(seconds))
    eng = engine([HOME], [Turn(tap="0")], policy=Policy(settle_seconds=0.5))
    eng.step(goal="open something")
    assert naps == [0.5, 0.5]


# --- a one-off question --------------------------------------------------------


class ProbabilityClient:
    """Answers `jev_check`'s single noul question with a fixed value."""

    def __init__(self, value: float) -> None:
        self.value = value

    def evaluate(self, state, questions, *, model: str = "jev-latest"):
        from jev_hands.core.jev_client import JevResponse

        return JevResponse(
            answers={"check": {"type": "noul", "noul": self.value}},
            model="jev-1.13.0",
            usage={"input_tokens": 10, "output_tokens": 1},
            latency_ms=7,
        )


@pytest.mark.parametrize(
    "probability,tier",
    [(0.97, M.TIER_YES), (0.75, M.TIER_YES), (0.5, M.TIER_UNSURE), (0.05, M.TIER_NO)],
)
def test_check_reads_the_probability_as_yes_unsure_or_no(probability, tier):
    eng = Engine(FakeAdapter([fixture_text(HOME)]), ProbabilityClient(probability), fast_policy())
    result = eng.check(question="is the home page showing?")
    assert result["probability"] == pytest.approx(probability)
    assert result["tier"] == tier
    assert result["valid"] is True
