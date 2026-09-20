"""Confidence tiers, settle timing and cross-checks.

No word in an element's name holds an action back or lifts the bar: whatever
passes the confidence tier is executed. Payment sheets and biometrics on the
phone are the last line, and a word list in here only caused early exits on a
layer that is meant to know nothing about any app. Marking the actions that
pay, send or delete is the planner's job, in the plan.

The numbers are calibrated, not guessed, but they are calibrated against one
model version - see `Thresholds` for which one and what to do when it moves.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from typing import Any, Dict, List, Optional

from .models import (
    ACTION_NONE,
    ACTION_TAP,
    ACTION_TYPE,
    TIER_ACT,
    TIER_ASK,
    TIER_NO,
    TIER_STOP,
    TIER_UNSURE,
    TIER_YES,
)

FLAG_CONTRADICTION = "contradiction"
FLAG_INVALID = "invalid"
FLAG_MODEL_DRIFT = "model_drift"
FLAG_UNVERIFIED_EXECUTE = "unverified_execute"

# How each contradiction `cross_checks` can report starts, paired with the
# short name a step carries as `contradiction_kind`. The messages are built in
# `cross_checks` and read back in `contradiction_kind`, so the two live next to
# each other and cannot drift apart.
TAP_BUT_NO_TARGET = "`action` says tap an element but `tap_target` says none"
NONE_BUT_STRONG_TARGET = "`action` says none but `tap_target` puts"
REACHED_WHILE_POINTING = (
    "`reached` is high while `action` still points strongly at an element"
)
CONTRADICTION_KINDS = (
    (TAP_BUT_NO_TARGET, "tap_but_no_target"),
    (NONE_BUT_STRONG_TARGET, "none_but_strong_target"),
    (REACHED_WHILE_POINTING, "reached_while_pointing"),
)

# Where the two `reached` confirmation bars come from. The note and the two
# numbers move together; `scripts/calibrate_reached.py` produces both.
THRESHOLD_CALIBRATION = (
    "tests/fixtures/reached_calibration.jsonl, 56 labelled screens "
    "(26 destination, 30 not_destination) off validation rounds 5, 6 and 7, "
    "on jev-1.13.0, 2026-09-20. The sweep is in "
    "tests/fixtures/reached_calibration_result.json."
)
# `reached_entry` is what actually separates the two groups: destinations read
# 0.07 to 0.40 and screens that only lead somewhere read 0.47 to 0.94, and
# among the samples the `reached` gate lets through the bands are 0.07 to 0.33
# and 0.47 to 0.94. 0.40 sits in the middle of that empty band. It is the one
# bar the stop is read off, together with `reached`: 0 false stops, 2 misses on
# the dataset below, and both misses are screens the `reached` gate itself
# refused at 0.22, not screens this bar got wrong.
REACHED_ENTRY_DEFAULT = 0.4
# `reached_content` opens the gate and nothing else. It overlaps (destinations
# 0.38 to 0.97, the rest 0.04 to 0.50) so it cannot carry the decision on its
# own, and round 8 measured the overlap wider still on 60 fresh screens: as a
# confirmation bar it refused three screens that were the destination and
# caught nothing `reached_entry` had not already caught. What it is good for is
# the other end - a destination whose `reached` reads low is invisible without
# it - so 0.40 stays, as the second way into the gate.
REACHED_CONTENT_DEFAULT = 0.4


@dataclass
class Thresholds:
    """Where each probability is cut.

    `act` and `ask` come from roughly 100 calls over validation rounds 1 and 2.
    `reached` and `check` were calibrated on `jev-1.13.0` (2026-09-19, round 4
    batch B): 10 positive destination screens read 0.33 to 0.97 and 8 negative
    ones read 0.02 to 0.05, leaving the band 0.05 to 0.33 empty, and the highest
    reading on a screen that should have answered no was 0.15 - hence 0.25.
    The same samples put `jev_check` between 0.64 and 0.86, hence 0.75.

    `reached` and `reached_entry` are the two bars the stop is read off:
    confirmed means `reached` at or over its bar and `reached_entry` at or
    under its. `reached_content` is a gate opener only - it decides whether
    the readings are worth taking, never whether the step stops.
    THRESHOLD_CALIBRATION below says which dataset, which model and which
    day they were measured on; regenerate the fixture and rerun
    `scripts/calibrate_reached.py` when any of the three moves.

    A `model_drift` flag on a decision means the model that answered is not the
    one these were set on: replay the fixtures and calibrate again.
    """

    act: float = 0.65
    ask: float = 0.45
    popup: float = 0.5
    reached: float = 0.25
    check: float = 0.75
    reached_content: float = REACHED_CONTENT_DEFAULT
    reached_entry: float = REACHED_ENTRY_DEFAULT

    def to_json(self) -> Dict[str, float]:
        return {
            "act": self.act,
            "ask": self.ask,
            "popup": self.popup,
            "reached": self.reached,
            "check": self.check,
            "reached_content": self.reached_content,
            "reached_entry": self.reached_entry,
        }


@dataclass
class Policy:
    thresholds: Thresholds = field(default_factory=Thresholds)
    # Settling happens in two parts. The first wait is short and always paid;
    # the screen is then read, and only a screen that still looks identical to
    # the one before the action is given the second wait. A page that has
    # already moved is not worth waiting for.
    settle_min_seconds: float = 0.35
    settle_extra_seconds: float = 0.45
    # Paid after an action that did change the screen, before reading it a
    # second time, so that only a screen two reads agree on is handed to the
    # next step. Off by default: round 6 measured it on a phone and it cost
    # about 750 ms on 92% of the executed steps and blocked 62% of the reuse,
    # to save the four or five seconds of `stale_screen` retries that reuse had
    # cost over ten runs. Zero hands the single post-action read straight on,
    # and the freshness check before the next action stays the one that
    # protects the tap. Set it to try the trade again.
    settle_confirm_seconds: float = 0.0
    token_budget: int = 1500
    max_steps: int = 5
    model: str = "jev-latest"
    calibrated_model: str = "jev-1.13.0"
    # Re-read the screen and compare fingerprints immediately before acting.
    # Costs one extra tree read per step and is worth it: acting on a screen
    # that has moved on is the one mistake with no way back.
    verify_before_act: bool = True
    # Legacy name for the settle time, from when it was one fixed wait. It is
    # accepted everywhere the two new fields are and sets both of them to the
    # value given, so an old `settle_seconds: 0.8` still waits at least 0.8 s
    # and waits another 0.8 s when the screen has not moved.
    settle_seconds: InitVar[Optional[float]] = None

    def __post_init__(self, settle_seconds: Optional[float]) -> None:
        if settle_seconds is not None:
            self.settle_min_seconds = float(settle_seconds)
            self.settle_extra_seconds = float(settle_seconds)

    @property
    def settle_total_seconds(self) -> float:
        """The longest a step can spend settling. What a one-off wait with no
        screen to compare against - starting an app - should use."""
        return self.settle_min_seconds + self.settle_extra_seconds

    def to_json(self) -> Dict[str, Any]:
        return {
            "thresholds": self.thresholds.to_json(),
            "settle_min_seconds": self.settle_min_seconds,
            "settle_extra_seconds": self.settle_extra_seconds,
            "settle_confirm_seconds": self.settle_confirm_seconds,
            "token_budget": self.token_budget,
            "max_steps": self.max_steps,
            "model": self.model,
            "calibrated_model": self.calibrated_model,
            "verify_before_act": self.verify_before_act,
        }


def tier_for(confidence: float, policy: Policy) -> str:
    """Three tiers, one cut each. Nothing lifts or lowers them per element."""
    if confidence >= policy.thresholds.act:
        return TIER_ACT
    if confidence >= policy.thresholds.ask:
        return TIER_ASK
    return TIER_STOP


def check_tier(probability: float, policy: Policy) -> str:
    """Read a one-off yes/no probability as `yes`, `unsure` or `no`.

    The low cut mirrors the high one, so the default 0.75 answers `no` at 0.25
    and leaves 0.25 to 0.75 as `unsure`. Both ends then sit outside the band the
    calibration left empty, and one setting still moves both.
    """
    high = policy.thresholds.check
    low = round(1.0 - high, 10)
    if probability >= high:
        return TIER_YES
    if probability <= low:
        return TIER_NO
    return TIER_UNSURE


def top3(probabilities: Dict[str, float]) -> List[List[Any]]:
    ordered = sorted(probabilities.items(), key=lambda kv: (-kv[1], kv[0]))
    return [[key, round(float(value), 4)] for key, value in ordered[:3]]


def cross_checks(
    *,
    action: str,
    action_confidence: float,
    tap_choice: Optional[str],
    tap_probabilities: Optional[Dict[str, float]],
    reached: Optional[float],
    policy: Policy,
) -> List[str]:
    """Three ways the answers in one response can contradict each other.

    When they do, nothing is executed and the caller decides.
    """
    problems: List[str] = []

    if action == ACTION_TAP and tap_choice == "none":
        problems.append(TAP_BUT_NO_TARGET)

    if action == ACTION_NONE and tap_probabilities:
        best = max(tap_probabilities.items(), key=lambda kv: kv[1], default=None)
        if best and best[0] != "none" and best[1] >= 0.8:
            problems.append(
                f"{NONE_BUT_STRONG_TARGET} {best[1]:.2f} on element {best[0]}"
            )

    if (
        reached is not None
        and reached >= policy.thresholds.reached
        and reached >= 0.8
        and action in (ACTION_TAP, ACTION_TYPE)
        and action_confidence >= 0.8
    ):
        problems.append(REACHED_WHILE_POINTING)

    return problems


def contradiction_kind(contradictions: List[str]) -> Optional[str]:
    """Short name for the contradiction that ended a step, or None for none.

    Batch G picked one kind - `action: none` with a strong `tap_target` - as
    the one that fires on a finished screen and reported the `end_state`
    readings only for it. Rounds 7 and 8 then saw four contradictions on a
    phone and all four were the other way round: `action: tap_element` with
    `tap_target: none`. The loop no longer singles a kind out; it reports
    which one fired, so the caller can tell them apart without reading the
    hint text.
    """
    for problem in contradictions:
        for message, kind in CONTRADICTION_KINDS:
            if problem.startswith(message):
                return kind
    return None


def model_drift(returned_model: Optional[str], policy: Policy) -> bool:
    """True when the model that answered is not the one the thresholds were set
    on. The thresholds may need recalibrating; replay the fixtures to find out."""
    if not returned_model or not policy.calibrated_model:
        return False
    return returned_model != policy.calibrated_model
