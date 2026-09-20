"""All six answer checks, each with its own failing branch."""

from __future__ import annotations

from jev_hands.core import validate as V

OPTIONS = ["0", "1", "2", "none"]


def good_answer():
    return {
        "type": "choice",
        "choice": "1",
        "probabilities": {"0": 0.1, "1": 0.7, "2": 0.15, "none": 0.05},
        "confidence": 0.7,
    }


def codes(failures):
    return [f.check for f in failures]


def test_a_well_formed_answer_passes_every_check():
    assert V.validate_choice("tap_target", good_answer(), OPTIONS) == []


def test_check_1_choice_must_be_one_of_the_options():
    answer = good_answer()
    answer["choice"] = "9"
    assert V.CHECK_CHOICE_IN_OPTIONS in codes(V.validate_choice("tap_target", answer, OPTIONS))


def test_check_2_probability_keys_must_equal_the_option_set():
    missing = good_answer()
    del missing["probabilities"]["2"]
    missing["probabilities"]["none"] = 0.2
    assert V.CHECK_KEYS_MATCH in codes(V.validate_choice("tap_target", missing, OPTIONS))

    extra = good_answer()
    extra["probabilities"]["7"] = 0.0
    assert V.CHECK_KEYS_MATCH in codes(V.validate_choice("tap_target", extra, OPTIONS))


def test_check_3_values_must_be_finite_and_within_zero_to_one():
    answer = good_answer()
    answer["probabilities"]["0"] = 1.4
    assert V.CHECK_VALUES_IN_RANGE in codes(V.validate_choice("tap_target", answer, OPTIONS))

    infinite = good_answer()
    infinite["probabilities"]["0"] = float("inf")
    assert V.CHECK_VALUES_IN_RANGE in codes(V.validate_choice("tap_target", infinite, OPTIONS))

    text = good_answer()
    text["probabilities"]["0"] = "high"
    assert V.CHECK_VALUES_IN_RANGE in codes(V.validate_choice("tap_target", text, OPTIONS))


def test_check_4_probabilities_must_sum_to_one():
    answer = good_answer()
    answer["probabilities"] = {"0": 0.1, "1": 0.2, "2": 0.1, "none": 0.1}
    assert V.CHECK_SUM_TO_ONE in codes(V.validate_choice("tap_target", answer, OPTIONS))


def test_a_rounding_sized_gap_is_tolerated():
    answer = good_answer()
    answer["probabilities"] = {"0": 0.1, "1": 0.7, "2": 0.15, "none": 0.04}
    assert V.CHECK_SUM_TO_ONE not in codes(V.validate_choice("tap_target", answer, OPTIONS))


def test_check_5_the_chosen_option_must_be_the_most_probable_one():
    answer = good_answer()
    answer["choice"] = "0"
    assert V.CHECK_CHOICE_IS_ARGMAX in codes(V.validate_choice("tap_target", answer, OPTIONS))


def test_check_6_the_answer_must_belong_to_the_current_observation():
    failures = V.validate_answers(
        {"tap_target": good_answer()},
        {"tap_target": OPTIONS},
        answer_capture_id="c_old",
        current_capture_id="c_new",
    )
    assert V.CHECK_CAPTURE_ID in codes(failures)

    matching = V.validate_answers(
        {"tap_target": good_answer()},
        {"tap_target": OPTIONS},
        answer_capture_id="c_same",
        current_capture_id="c_same",
    )
    assert matching == []


def test_every_check_has_a_failing_branch_covered():
    assert set(V.ALL_CHECKS) == {
        V.CHECK_CHOICE_IN_OPTIONS,
        V.CHECK_KEYS_MATCH,
        V.CHECK_VALUES_IN_RANGE,
        V.CHECK_SUM_TO_ONE,
        V.CHECK_CHOICE_IS_ARGMAX,
        V.CHECK_CAPTURE_ID,
    }


def test_noul_answers_are_range_checked():
    assert V.validate_noul("blocking_popup", {"type": "noul", "noul": 0.4}) == []
    assert V.validate_noul("blocking_popup", {"type": "noul", "noul": 1.4})
    assert V.validate_noul("blocking_popup", {"type": "noul", "noul": None})
    assert V.validate_noul("blocking_popup", {"type": "noul", "noul": True})


def test_missing_probabilities_object_fails_loudly():
    failures = V.validate_choice("tap_target", {"type": "choice", "choice": "1"}, OPTIONS)
    assert V.CHECK_KEYS_MATCH in codes(failures)


def test_an_answer_with_no_type_is_a_failure_not_a_silent_pass():
    """An answer whose shape we do not recognise used to run zero checks and
    count as clean. That is the one way a malformed answer reaches a real tap."""
    failures = V.validate_answers({"tap_target": {"choice": "1"}}, {"tap_target": OPTIONS})
    assert V.CHECK_ANSWER_TYPE in codes(failures)


def test_an_answer_with_an_unknown_type_is_a_failure():
    failures = V.validate_answers(
        {"frustration": {"type": "score", "score": 1.6}}, {"tap_target": OPTIONS}
    )
    assert V.CHECK_ANSWER_TYPE in codes(failures)


def test_a_question_with_no_offered_options_is_a_failure():
    failures = V.validate_answers({"mystery": good_answer()}, {})
    assert V.CHECK_CHOICE_IN_OPTIONS in codes(failures)
