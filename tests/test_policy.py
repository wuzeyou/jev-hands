"""Confidence tiers, the check tiers, settle timing and the cross-checks."""

from __future__ import annotations

import pytest

from jev_hands.core import policy as P
from jev_hands.core.models import (
    ACTION_NONE,
    ACTION_TAP,
    TIER_ACT,
    TIER_ASK,
    TIER_NO,
    TIER_STOP,
    TIER_UNSURE,
    TIER_YES,
)


@pytest.mark.parametrize(
    "confidence,expected",
    [(0.99, TIER_ACT), (0.65, TIER_ACT), (0.6499, TIER_ASK), (0.45, TIER_ASK), (0.4499, TIER_STOP), (0.0, TIER_STOP)],
)
def test_three_tiers_at_their_boundaries(confidence, expected):
    assert P.tier_for(confidence, P.Policy()) == expected


def test_nothing_about_the_element_moves_the_tier():
    """A payment button and a settings row are read off exactly the same
    scale; marking the ones that matter is the plan's job. The names are
    spelled in pieces so that a grep for the removed mechanism finds nothing
    but the fixtures."""
    policy = P.Policy()
    assert P.tier_for(0.8, policy) == TIER_ACT
    for gone in ("deny" + "list", "risk" + "_words", "risk" + "_act"):
        assert not hasattr(policy, gone)
        assert not hasattr(policy.thresholds, gone)
        assert gone not in policy.to_json()
        assert gone not in policy.to_json()["thresholds"]


def test_thresholds_are_configurable():
    policy = P.Policy(thresholds=P.Thresholds(act=0.9, ask=0.8))
    assert P.tier_for(0.85, policy) == TIER_ASK
    assert P.tier_for(0.95, policy) == TIER_ACT
    assert P.tier_for(0.7, policy) == TIER_STOP


def test_the_calibrated_defaults():
    """Calibrated on jev-1.13.0; a model_drift flag means do it again."""
    thresholds = P.Thresholds()
    assert thresholds.reached == 0.25
    assert thresholds.check == 0.75
    assert P.Policy().calibrated_model == "jev-1.13.0"


def test_the_two_confirmations_have_their_own_bars_and_do_not_borrow_check():
    """Round 7 showed one confirmation question could not separate the two
    groups at any threshold. Batch G split it in two and measured both bars on
    56 labelled screens; see THRESHOLD_CALIBRATION."""
    thresholds = P.Thresholds()
    assert thresholds.reached_content == 0.4
    assert thresholds.reached_entry == 0.4
    assert thresholds.reached_content != thresholds.check
    assert thresholds.to_json()["reached_content"] == 0.4
    assert thresholds.to_json()["reached_entry"] == 0.4
    assert P.Thresholds(check=0.9).reached_content == 0.4
    assert "reached_calibration.jsonl" in P.THRESHOLD_CALIBRATION
    assert "jev-1.13.0" in P.THRESHOLD_CALIBRATION


@pytest.mark.parametrize(
    "probability,expected",
    [(0.97, TIER_YES), (0.75, TIER_YES), (0.7499, TIER_UNSURE), (0.26, TIER_UNSURE),
     (0.25, TIER_NO), (0.02, TIER_NO)],
)
def test_check_reads_as_yes_unsure_or_no(probability, expected):
    assert P.check_tier(probability, P.Policy()) == expected


def test_moving_the_check_threshold_moves_both_ends():
    policy = P.Policy(thresholds=P.Thresholds(check=0.9))
    assert P.check_tier(0.85, policy) == TIER_UNSURE
    assert P.check_tier(0.95, policy) == TIER_YES
    assert P.check_tier(0.1, policy) == TIER_NO
    assert P.check_tier(0.11, policy) == TIER_UNSURE


def test_settling_is_two_waits_with_the_old_name_still_accepted():
    policy = P.Policy()
    assert (policy.settle_min_seconds, policy.settle_extra_seconds) == (0.35, 0.45)
    assert policy.settle_total_seconds == pytest.approx(0.8)

    legacy = P.Policy(settle_seconds=1.2)
    assert legacy.settle_min_seconds == 1.2
    assert legacy.settle_extra_seconds == 1.2
    assert "settle_seconds" not in legacy.to_json()
    assert legacy.to_json()["settle_min_seconds"] == 1.2


def test_the_stability_wait_stands_on_its_own():
    """It is paid on the branch the other two are not - after an action that
    did change the screen - so the old one-wait name must not touch it."""
    policy = P.Policy()
    assert policy.settle_confirm_seconds == 0.0, "off by default after round 6"
    assert policy.to_json()["settle_confirm_seconds"] == 0.0
    assert P.Policy(settle_seconds=1.2).settle_confirm_seconds == 0.0
    assert P.Policy(settle_confirm_seconds=0.25).settle_confirm_seconds == 0.25


def test_verify_before_act_is_on_by_default():
    assert P.Policy().verify_before_act is True
    assert P.Policy().to_json()["verify_before_act"] is True


def test_cross_check_tap_with_no_target():
    problems = P.cross_checks(
        action=ACTION_TAP,
        action_confidence=0.9,
        tap_choice="none",
        tap_probabilities={"none": 0.9, "0": 0.1},
        reached=None,
        policy=P.Policy(),
    )
    assert problems and "tap an element" in problems[0]


def test_cross_check_none_while_an_element_is_almost_certain():
    problems = P.cross_checks(
        action=ACTION_NONE,
        action_confidence=0.6,
        tap_choice="3",
        tap_probabilities={"3": 0.85, "none": 0.15},
        reached=None,
        policy=P.Policy(),
    )
    assert problems and "says none" in problems[0]

    quiet = P.cross_checks(
        action=ACTION_NONE,
        action_confidence=0.6,
        tap_choice="3",
        tap_probabilities={"3": 0.6, "none": 0.4},
        reached=None,
        policy=P.Policy(),
    )
    assert quiet == []


def test_cross_check_reached_while_still_pointing_at_an_element():
    problems = P.cross_checks(
        action=ACTION_TAP,
        action_confidence=0.9,
        tap_choice="2",
        tap_probabilities={"2": 0.9, "none": 0.1},
        reached=0.88,
        policy=P.Policy(),
    )
    assert problems and "reached" in problems[0]


def test_no_cross_check_fires_on_a_clean_answer():
    assert P.cross_checks(
        action=ACTION_TAP,
        action_confidence=0.95,
        tap_choice="4",
        tap_probabilities={"4": 0.9, "none": 0.1},
        reached=0.1,
        policy=P.Policy(),
    ) == []


def test_model_drift_is_flagged_when_the_answering_model_moved():
    policy = P.Policy(calibrated_model="jev-1.13.0")
    assert P.model_drift("jev-1.14.0", policy) is True
    assert P.model_drift("jev-1.13.0", policy) is False
    assert P.model_drift(None, policy) is False


def test_top3_is_sorted_and_capped():
    result = P.top3({"a": 0.1, "b": 0.5, "c": 0.3, "d": 0.1})
    assert result == [["b", 0.5], ["c", 0.3], ["a", 0.1]]
