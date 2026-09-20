"""Six checks every model answer must pass before anything is executed.

Two validation rounds produced 84 choice answers and all of them passed, but the
layer stays: one malformed answer that slips through turns into a tap on a real
phone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

PROBABILITY_SUM_TOLERANCE = 0.02

CHECK_CHOICE_IN_OPTIONS = "choice_not_in_options"
CHECK_KEYS_MATCH = "probability_keys_mismatch"
CHECK_VALUES_IN_RANGE = "probability_out_of_range"
CHECK_SUM_TO_ONE = "probabilities_do_not_sum_to_one"
CHECK_CHOICE_IS_ARGMAX = "choice_is_not_argmax"
CHECK_CAPTURE_ID = "stale_capture_id"
CHECK_ANSWER_TYPE = "unknown_answer_type"

ALL_CHECKS = (
    CHECK_CHOICE_IN_OPTIONS,
    CHECK_KEYS_MATCH,
    CHECK_VALUES_IN_RANGE,
    CHECK_SUM_TO_ONE,
    CHECK_CHOICE_IS_ARGMAX,
    CHECK_CAPTURE_ID,
)


@dataclass
class Failure:
    check: str
    question: str
    detail: str

    def to_json(self) -> Dict[str, str]:
        return {"check": self.check, "question": self.question, "detail": self.detail}


def validate_choice(
    question_id: str,
    answer: Dict[str, Any],
    options: Sequence[str],
) -> List[Failure]:
    """Checks 1 to 5 for one choice answer."""
    failures: List[Failure] = []
    option_set = set(options)
    choice = answer.get("choice")
    probabilities = answer.get("probabilities")

    if choice not in option_set:
        failures.append(
            Failure(
                CHECK_CHOICE_IN_OPTIONS,
                question_id,
                f"choice {choice!r} is not one of the {len(option_set)} offered options",
            )
        )

    if not isinstance(probabilities, dict):
        failures.append(
            Failure(CHECK_KEYS_MATCH, question_id, "probabilities missing or not an object")
        )
        return failures

    if set(probabilities) != option_set:
        missing = sorted(option_set - set(probabilities))
        extra = sorted(set(probabilities) - option_set)
        failures.append(
            Failure(
                CHECK_KEYS_MATCH,
                question_id,
                f"probability keys differ from the options (missing={missing}, extra={extra})",
            )
        )

    numeric: Dict[str, float] = {}
    bad_values = []
    for key, value in probabilities.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            bad_values.append(key)
            continue
        value = float(value)
        if not math.isfinite(value) or value < 0.0 or value > 1.0:
            bad_values.append(key)
            continue
        numeric[key] = value
    if bad_values:
        failures.append(
            Failure(
                CHECK_VALUES_IN_RANGE,
                question_id,
                f"probabilities not finite numbers in [0, 1]: {sorted(bad_values)}",
            )
        )

    if numeric:
        total = sum(numeric.values())
        if abs(total - 1.0) >= PROBABILITY_SUM_TOLERANCE:
            failures.append(
                Failure(
                    CHECK_SUM_TO_ONE,
                    question_id,
                    f"probabilities sum to {total:.4f}, not 1 within {PROBABILITY_SUM_TOLERANCE}",
                )
            )

        if choice in numeric:
            best = max(numeric.values())
            if numeric[choice] < best - 1e-9:
                failures.append(
                    Failure(
                        CHECK_CHOICE_IS_ARGMAX,
                        question_id,
                        f"choice {choice!r} at {numeric[choice]:.4f} is below the top probability {best:.4f}",
                    )
                )

    return failures


def validate_noul(question_id: str, answer: Dict[str, Any]) -> List[Failure]:
    value = answer.get("noul")
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        return [
            Failure(
                CHECK_VALUES_IN_RANGE,
                question_id,
                f"noul value {value!r} is not a finite number in [0, 1]",
            )
        ]
    return []


def validate_answers(
    answers: Dict[str, Dict[str, Any]],
    option_sets: Dict[str, Sequence[str]],
    *,
    answer_capture_id: Optional[str] = None,
    current_capture_id: Optional[str] = None,
) -> List[Failure]:
    """Run all six checks over one response."""
    failures: List[Failure] = []

    if current_capture_id is not None and answer_capture_id != current_capture_id:
        failures.append(
            Failure(
                CHECK_CAPTURE_ID,
                "*",
                "the answer belongs to an earlier observation; the screen has moved on",
            )
        )

    for question_id, answer in answers.items():
        if not isinstance(answer, dict):
            failures.append(
                Failure(CHECK_KEYS_MATCH, question_id, "answer is not an object")
            )
            continue
        kind = answer.get("type")
        if kind == "choice":
            options = option_sets.get(question_id)
            if options is None:
                failures.append(
                    Failure(
                        CHECK_CHOICE_IN_OPTIONS,
                        question_id,
                        "no option set was offered for this question",
                    )
                )
                continue
            failures.extend(validate_choice(question_id, answer, options))
        elif kind == "noul":
            failures.extend(validate_noul(question_id, answer))
        else:
            # Anything else means the response shape is not what we asked for.
            # Running zero checks and calling that a pass is how a malformed
            # answer would slip through to a real tap.
            failures.append(
                Failure(
                    CHECK_ANSWER_TYPE,
                    question_id,
                    f"answer type {kind!r} is neither 'choice' nor 'noul'",
                )
            )

    return failures
