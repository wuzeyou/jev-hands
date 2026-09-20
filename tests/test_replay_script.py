"""The replay harness, exercised without calling the API.

The important assertion here is that every expected element name still exists in
its fixture. The candidate builder renumbers and renames rows whenever its rules
change, so ground truth written months ago rots silently otherwise.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_replay():
    spec = importlib.util.spec_from_file_location(
        "replay_decide", ROOT / "scripts" / "replay_decide.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["replay_decide"] = module
    spec.loader.exec_module(module)
    return module


replay = load_replay()


def test_the_case_table_is_not_empty_and_has_unique_keys():
    keys = [case.key for case in replay.CASES]
    assert len(keys) >= 15
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("case", replay.CASES, ids=lambda c: c.key)
def test_every_expected_element_still_exists_in_its_fixture(case):
    from jev_hands.core import candidates as candidates_mod

    screen = candidates_mod.build_screen(
        (replay.FIXTURES / case.fixture).read_text(encoding="utf-8"),
        scroll={"can_down": True, "can_up": False},
    )
    available = {e.name for e in screen.elements} | {"none"}
    missing = [name for name in case.expect if name not in available]
    assert not missing, f"{case.key}: ground truth {missing} is no longer on the screen"


@pytest.mark.parametrize("case", replay.CASES, ids=lambda c: c.key)
def test_every_case_produces_a_valid_question_set(case):
    from jev_hands.core import candidates as candidates_mod
    from jev_hands.core import questions as questions_mod

    screen = candidates_mod.build_screen(
        (replay.FIXTURES / case.fixture).read_text(encoding="utf-8"),
        scroll={"can_down": True, "can_up": False},
    )
    question_set = questions_mod.build_questions(
        screen, text_to_type=case.text_to_type, end_state=case.end_state
    )
    assert questions_mod.check_question_set(question_set) == []
    if case.expect_action:
        assert case.expect_action in question_set["action"]["criteria"], case.key


def test_scoring_marks_a_matching_answer_correct(monkeypatch):
    from jev_hands.core import candidates as candidates_mod
    from jev_hands.core import policy as policy_mod
    from jev_hands.core.jev_client import JevResponse

    case = next(c for c in replay.CASES if c.key == "home_delivery")
    screen = candidates_mod.build_screen(
        (replay.FIXTURES / case.fixture).read_text(encoding="utf-8"),
        scroll={"can_down": True, "can_up": False},
    )
    index = str(next(e.index for e in screen.elements if e.name == case.expect[0]))

    class StubClient:
        def evaluate(self, state, questions, *, model="jev-latest"):
            def choice(options, pick, confidence):
                rest = [o for o in options if o != pick]
                probabilities = {pick: confidence}
                for option in rest:
                    probabilities[option] = (1 - confidence) / len(rest)
                return {
                    "type": "choice",
                    "choice": pick,
                    "probabilities": probabilities,
                    "confidence": confidence,
                }

            return JevResponse(
                answers={
                    "action": choice(list(questions["action"]["criteria"]), "tap_element", 0.98),
                    "tap_target": choice(list(questions["tap_target"]["criteria"]), index, 0.93),
                    "blocking_popup": {"type": "noul", "noul": 0.04},
                },
                model="jev-1.13.0",
                usage={"input_tokens": 900},
                latency_ms=700,
            )

    outcome = replay.run_case(StubClient(), case, policy_mod.Policy())
    assert outcome.correct is True
    assert outcome.chosen == case.expect[0]
    assert outcome.tier == "act"
    assert outcome.invalid == []


def test_scoring_marks_the_wrong_element_incorrect():
    from jev_hands.core import policy as policy_mod
    from jev_hands.core.jev_client import JevResponse

    case = next(c for c in replay.CASES if c.key == "home_delivery")

    class StubClient:
        def evaluate(self, state, questions, *, model="jev-latest"):
            def choice(options, pick, confidence):
                rest = [o for o in options if o != pick]
                probabilities = {pick: confidence}
                for option in rest:
                    probabilities[option] = (1 - confidence) / len(rest)
                return {
                    "type": "choice",
                    "choice": pick,
                    "probabilities": probabilities,
                    "confidence": confidence,
                }

            return JevResponse(
                answers={
                    "action": choice(list(questions["action"]["criteria"]), "tap_element", 0.9),
                    "tap_target": choice(list(questions["tap_target"]["criteria"]), "none", 0.6),
                    "blocking_popup": {"type": "noul", "noul": 0.04},
                },
                model="jev-1.13.0",
                usage={"input_tokens": 900},
                latency_ms=700,
            )

    outcome = replay.run_case(StubClient(), case, policy_mod.Policy())
    assert outcome.correct is False
    assert outcome.chosen == "none"
