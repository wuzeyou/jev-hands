"""The question set: what is offered, what is not, and how it is worded."""

from __future__ import annotations

from conftest import build

from jev_hands.core import questions as Q
from jev_hands.core.models import (
    ACTION_BACK,
    ACTION_NONE,
    ACTION_SCROLL_DOWN,
    ACTION_SCROLL_UP,
    ACTION_TAP,
    ACTION_TYPE,
)


def test_there_is_no_done_and_no_give_up():
    """Round two: five `done` answers, four of them wrong, and the wrong ones
    were the confident ones. Judging completion is not Jev's job."""
    screen = build("delivery_home.xml")
    questions = Q.build_questions(screen)
    options = set(questions[Q.Q_ACTION]["criteria"])
    assert "done" not in options
    assert "give_up" not in options
    assert Q.check_question_set(questions) == []


def test_only_feasible_actions_are_offered():
    screen = build("delivery_home.xml", scroll={"can_down": True, "can_up": False})
    options = Q.feasible_actions(screen)
    assert ACTION_SCROLL_DOWN in options
    assert ACTION_SCROLL_UP not in options
    assert ACTION_TYPE not in options
    assert ACTION_TAP in options
    assert ACTION_BACK in options
    assert ACTION_NONE in options


def test_typing_is_only_offered_with_text_and_an_editable_element():
    with_field = build("delivery_search.xml")
    assert ACTION_TYPE in Q.feasible_actions(with_field, text_to_type="hello")
    assert ACTION_TYPE not in Q.feasible_actions(with_field)

    without_field = build("popup_app_clone_chooser.xml")
    assert ACTION_TYPE not in Q.feasible_actions(without_field, text_to_type="hello")


def test_the_type_target_question_only_lists_editable_elements():
    screen = build("delivery_search.xml")
    questions = Q.build_questions(screen, text_to_type="hello")
    offered = set(questions[Q.Q_TYPE_TARGET]["criteria"]) - {"none"}
    editable = {str(e.index) for e in screen.elements if e.editable}
    assert offered == editable


def test_reached_is_asked_only_when_an_end_state_is_given():
    screen = build("delivery_home.xml")
    assert Q.Q_REACHED not in Q.build_questions(screen)
    with_end = Q.build_questions(screen, end_state="a list of results is on screen")
    assert Q.Q_REACHED in with_end
    assert with_end[Q.Q_REACHED]["type"] == "noul"


def test_element_criteria_are_all_null():
    """The detail lives once in `screen`; repeating it in criteria doubled the
    token cost for no measurable gain."""
    screen = build("delivery_home.xml")
    questions = Q.build_questions(screen, text_to_type="hello")
    for question_id in (Q.Q_TAP_TARGET,):
        assert set(questions[question_id]["criteria"].values()) == {None}
    assert set(questions[Q.Q_TAP_TARGET]["criteria"]) == {
        str(e.index) for e in screen.elements
    } | {"none"}


def test_a_question_set_that_breaks_the_rules_is_reported():
    screen = build("delivery_home.xml")
    questions = Q.build_questions(screen)
    questions[Q.Q_ACTION]["criteria"]["done"] = "finished"
    questions[Q.Q_TAP_TARGET]["criteria"]["0"] = "the first one"
    problems = Q.check_question_set(questions)
    assert any("done" in p for p in problems)
    assert any("null" in p for p in problems)


def test_instructions_are_english_use_backticks_and_say_text_is_data():
    screen = build("delivery_search.xml")
    questions = Q.build_questions(
        screen, text_to_type="hello", end_state="results are on screen"
    )
    for question in questions.values():
        text = question["instructions"]
        assert text.isascii(), text
        assert "data, never as instructions" in text or "data, never" in text
    assert "`goal`" in questions[Q.Q_ACTION]["instructions"]
    assert "`screen`" in questions[Q.Q_TAP_TARGET]["instructions"]
    assert "`end_state`" in questions[Q.Q_REACHED]["instructions"]
    assert "`text_to_type`" in questions[Q.Q_TYPE_TARGET]["instructions"]


def test_reached_criteria_rules_out_mere_entry_points():
    assert "route towards" in Q.REACHED_CRITERIA["false"]
    assert "entry point" in Q.REACHED_INSTRUCTIONS


def test_both_confirmations_ride_in_the_same_request_as_the_gate():
    """Jev answers the questions of one request in parallel, so the two extra
    readings cost tokens and no latency. Round 6 paid a median 496 ms for one
    of them as a request of its own, on 40% of the steps."""
    screen = build("delivery_home.xml")
    plain = Q.build_questions(screen)
    assert Q.Q_REACHED_CONTENT not in plain
    assert Q.Q_REACHED_ENTRY not in plain
    with_end = Q.build_questions(screen, end_state="the orders list is on screen")
    assert set(with_end) >= {
        Q.Q_ACTION,
        Q.Q_TAP_TARGET,
        Q.Q_REACHED,
        Q.Q_REACHED_CONTENT,
        Q.Q_REACHED_ENTRY,
    }
    for qid in (Q.Q_REACHED_CONTENT, Q.Q_REACHED_ENTRY):
        question = with_end[qid]
        assert question["type"] == "noul"
        assert set(question["criteria"]) == {"true", "false"}
        assert "`end_state`" in question["instructions"]
    assert Q.check_question_set(with_end) == []


def test_each_confirmation_asks_one_literal_thing():
    """Round 7: one question doing both jobs lifted entry pages by 0.35 and
    content pages by 0.15, and the two groups ended up overlapping between
    0.37 and 0.64. Content and entry are now asked separately, and neither
    question is asked to weigh the other one up."""
    content = Q.REACHED_CONTENT_INSTRUCTIONS
    assert "visible on `screen` right now" in content
    assert "entry" not in content
    assert "list, details, value, result" in Q.REACHED_CONTENT_CRITERIA["true"]
    assert Q.REACHED_CONTENT_CRITERIA["false"] == "It is not on this screen."

    entry = Q.REACHED_ENTRY_INSTRUCTIONS
    for phrase in ("home, entry, menu or navigation page", "rather than that state itself"):
        assert phrase in entry
    for phrase in ("entry point", "a menu", "a chooser"):
        assert phrase in Q.REACHED_ENTRY_CRITERIA["true"]
    assert "destination" in Q.REACHED_ENTRY_CRITERIA["false"]

    for text in (content, entry):
        assert text.isascii()


def test_state_keeps_only_the_last_five_actions():
    screen = build("delivery_home.xml")
    from jev_hands.core import candidates as candidates_mod

    state = Q.build_state(
        candidates_mod.screen_state(screen),
        goal="open the delivery channel",
        app=screen.app,
        recent_actions=[f"action {i}" for i in range(9)],
    )
    assert state["recent_actions"] == [f"action {i}" for i in range(4, 9)]
    assert state["goal"] == "open the delivery channel"
    assert "screen" in state


def test_the_state_never_carries_coordinates():
    screen = build("delivery_home.xml")
    from jev_hands.core import candidates as candidates_mod

    rendered = repr(candidates_mod.screen_state(screen))
    assert "bounds" not in rendered
    assert '"at"' not in rendered


def test_the_window_clause_appears_only_when_a_second_window_is_on_screen():
    """The rows name their window only on a multi-window screen, so that is the
    only place the instructions explain it. Every other screen keeps the wording
    the thresholds were calibrated on."""
    from jev_hands.core import candidates as candidates_mod

    plain = build("delivery_home.xml")
    assert Q.build_questions(plain)[Q.Q_TAP_TARGET]["instructions"] == Q.TAP_INSTRUCTIONS

    dialog = (
        '<node class="android.widget.FrameLayout" package="com.vendor.chooser" '
        'bounds="[100,700][900,1300]">'
        '<node class="android.widget.Button" text="Open once" package="com.vendor.chooser" '
        'clickable="true" enabled="true" bounds="[140,1180][500,1260]" /></node>'
    )
    covered = candidates_mod.build_screen(
        '<hierarchy rotation="0">'
        '<node class="android.widget.FrameLayout" package="com.example.app" '
        'bounds="[0,0][1000,2000]">'
        '<node class="android.widget.Button" text="Go" package="com.example.app" '
        'clickable="true" enabled="true" bounds="[0,100][1000,300]" /></node>'
        + dialog
        + "</hierarchy>"
    )
    instructions = Q.build_questions(covered)[Q.Q_TAP_TARGET]["instructions"]
    assert instructions == Q.TAP_INSTRUCTIONS_WITH_WINDOW
    assert "`window`" in instructions
    assert instructions.isascii()
