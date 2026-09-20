"""Shared test helpers: fixture loading, a fake adapter and a fake Jev client."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

sys.dont_write_bytecode = True

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


from jev_hands.core import candidates as candidates_mod  # noqa: E402
from jev_hands.core.jev_client import JevResponse  # noqa: E402
from jev_hands.core.models import Screen  # noqa: E402
from jev_hands.errors import JevHandsError, ObserveError  # noqa: E402


def build(name: str, **kwargs) -> Screen:
    return candidates_mod.build_screen(fixture_text(name), **kwargs)


def names(screen: Screen) -> List[str]:
    return [e.name for e in screen.elements]


class FakeAdapter:
    """Replays recorded UI trees and records what would have been executed."""

    name = "fake"

    def __init__(
        self,
        sequence: Sequence[str],
        *,
        observe_errors: Optional[Sequence[bool]] = None,
        execute_error: bool = False,
        stale_after: Optional[int] = None,
    ) -> None:
        self.sequence = list(sequence)
        self.observe_errors = list(observe_errors or [])
        self.execute_error = execute_error
        # From this freshness check onwards the adapter reports that the screen
        # has moved on. None means it never does.
        self.stale_after = stale_after
        self.freshness_checks = 0
        self.screenshots = 0
        self.executed: List[Dict[str, Any]] = []
        self.observations = 0
        self.last_screen: Optional[Screen] = None

    def _next_xml(self) -> str:
        position = min(self.observations, len(self.sequence) - 1)
        return self.sequence[position]

    def observe(
        self,
        *,
        token_budget: int = 1500,
        scroll: Optional[Dict[str, bool]] = None,
        screenshot: bool = False,
        screenshot_dir: Optional[Path] = None,
    ) -> Screen:
        if self.observations < len(self.observe_errors) and self.observe_errors[self.observations]:
            self.observations += 1
            raise ObserveError("the recorded run says the tree could not be read")
        xml_text = self._next_xml()
        self.observations += 1
        screen = candidates_mod.build_screen(
            xml_text, token_budget=token_budget, scroll=scroll
        )
        self.last_screen = screen
        return screen

    def fresh(self, capture_id: str) -> bool:
        self.freshness_checks += 1
        if self.stale_after is not None and self.freshness_checks > self.stale_after:
            return False
        return bool(self.last_screen and self.last_screen.capture_id == capture_id)

    def screenshot(self, directory) -> str:
        self.screenshots += 1
        return f"{directory}/shot-{self.screenshots}.png"

    def execute(self, action: Dict[str, Any]) -> Dict[str, Any]:
        if self.execute_error:
            raise JevHandsError(
                "the recorded run says the action outcome is unknown",
                code="execute_uncertain",
                hint="observe again",
            )
        self.executed.append(action)
        return {"type": action.get("type")}


# The API derives `confidence` from the distribution, so the two are related but
# not equal. Keeping them apart here means any code that reaches for
# `probabilities[choice]` where it should read `confidence` fails a test instead
# of quietly agreeing.
CONFIDENCE_OFFSET = 0.04


def choice_answer(options: Sequence[str], choice: str, confidence: float) -> Dict[str, Any]:
    """A well formed choice answer: keys exactly the options, they sum to 1, the
    chosen option is the most probable one, and its probability is deliberately
    a little above the reported confidence."""
    top = min(0.99, confidence + CONFIDENCE_OFFSET)
    others = [o for o in options if o != choice]
    probabilities = {choice: top}
    if others:
        share = (1.0 - top) / len(others)
        for option in others:
            probabilities[option] = share
    else:
        probabilities[choice] = 1.0
    return {
        "type": "choice",
        "choice": choice,
        "probabilities": probabilities,
        "confidence": confidence,
    }


def noul_answer(value: float) -> Dict[str, Any]:
    return {"type": "noul", "noul": value}


class Turn:
    """One canned model response, described in terms of what it decides."""

    def __init__(
        self,
        *,
        action: str = "tap_element",
        action_confidence: float = 0.95,
        tap: Optional[str] = None,
        tap_confidence: float = 0.9,
        type_target: Optional[str] = None,
        type_confidence: float = 0.95,
        popup: float = 0.05,
        reached: float = 0.05,
        content: float = 0.9,
        entry: float = 0.05,
        broken: Optional[str] = None,
    ) -> None:
        self.action = action
        self.action_confidence = action_confidence
        self.tap = tap
        self.tap_confidence = tap_confidence
        self.type_target = type_target
        self.type_confidence = type_confidence
        self.popup = popup
        self.reached = reached
        # What the two literal confirmations in the same request answer. They
        # are read once the gate opens; the defaults back a gate up, so a turn
        # that wants a stop only has to say `reached`.
        self.content = content
        self.entry = entry
        self.broken = broken


class FakeClient:
    """Serves canned answers shaped to whatever question set it is given."""

    def __init__(self, turns: Sequence[Turn], *, model: str = "jev-1.13.0") -> None:
        self.turns = list(turns)
        self.model = model
        self.calls: List[Dict[str, Any]] = []
        # One-question requests from `Engine.check`. The step loop stopped
        # making them in batch F; a test asserting this list is empty is
        # asserting that the confirmation rode in the main request.
        self.checks: List[Dict[str, Any]] = []

    def evaluate(self, state, questions, *, model: str = "jev-latest") -> JevResponse:
        if set(questions) == {"check"}:
            # Belongs to the turn that was just decided, not to the next one.
            turn = self.turns[min(max(len(self.calls) - 1, 0), len(self.turns) - 1)]
            self.checks.append({"state": state, "questions": questions})
            return JevResponse(
                answers={"check": noul_answer(turn.content)},
                model=self.model,
                usage={"input_tokens": 234, "output_tokens": 2},
                latency_ms=11,
            )

        turn = self.turns[min(len(self.calls), len(self.turns) - 1)]
        self.calls.append({"state": state, "questions": questions})

        answers: Dict[str, Any] = {}
        action_options = list(questions["action"]["criteria"].keys())
        action = turn.action if turn.action in action_options else "none"
        answers["action"] = choice_answer(action_options, action, turn.action_confidence)

        tap_options = list(questions["tap_target"]["criteria"].keys())
        tap = turn.tap if turn.tap in tap_options else "none"
        answers["tap_target"] = choice_answer(tap_options, tap, turn.tap_confidence)

        if "type_target" in questions:
            type_options = list(questions["type_target"]["criteria"].keys())
            chosen = turn.type_target if turn.type_target in type_options else "none"
            answers["type_target"] = choice_answer(type_options, chosen, turn.type_confidence)

        answers["blocking_popup"] = noul_answer(turn.popup)
        if "reached" in questions:
            answers["reached"] = noul_answer(turn.reached)
        if "reached_content" in questions:
            answers["reached_content"] = noul_answer(turn.content)
        if "reached_entry" in questions:
            answers["reached_entry"] = noul_answer(turn.entry)

        if turn.broken == "sum":
            answers["tap_target"]["probabilities"] = {
                key: 0.9 for key in answers["tap_target"]["probabilities"]
            }
        elif turn.broken == "keys":
            answers["tap_target"]["probabilities"]["not_an_option"] = 0.0

        return JevResponse(
            answers=answers,
            model=self.model,
            usage={"input_tokens": 1234, "output_tokens": 12},
            latency_ms=42,
        )


@pytest.fixture
def home_xml() -> str:
    return fixture_text("delivery_home.xml")


@pytest.fixture
def settings_xml() -> str:
    return fixture_text("settings_home.xml")
