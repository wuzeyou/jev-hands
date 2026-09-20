"""The shipped `reached` bars, checked against the readings they were set on.

`scripts/calibrate_reached.py` sends every labelled screen in
`tests/fixtures/reached_calibration.jsonl` to the real model and writes what
came back, plus the whole threshold sweep, to
`tests/fixtures/reached_calibration_result.json`. Nothing here talks to the
network: it replays those recorded probabilities through the rule the loop
actually applies - `reached` over its bar and `reached_entry` under its - and
insists the defaults still buy what the comment in `policy.Thresholds` says
they buy.

The point of the test is the day the fixture is regenerated on a newer model.
If the two label groups stop separating, the confusion counts move and this
fails - instead of the plugin quietly shipping bars that no longer work.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jev_hands.core import policy as policy_mod
from jev_hands.core.loop import Engine
from jev_hands.core.models import Decision

RESULT_PATH = Path(__file__).parent / "fixtures" / "reached_calibration_result.json"
DATASET_PATH = Path(__file__).parent / "fixtures" / "reached_calibration.jsonl"

# What the shipped bars buy on the recorded readings, now that the stop is
# read off `reached` and `reached_entry` alone. Both misses are screens the
# `reached` gate itself refused at 0.22; `reached_entry` got neither wrong.
# The same two counts came out of the old rule, which had `reached_content`
# in the verdict as well: dropping it costs this dataset nothing, and round 8
# measured it winning four screens back on 60 fresh ones.
EXPECTED_FALSE_STOPS = 0
EXPECTED_MISSES = 2


@pytest.fixture(scope="module")
def recorded():
    return json.loads(RESULT_PATH.read_text(encoding="utf-8"))


def _engine(home_xml, *, entry=None):
    """An engine that only exists to be asked how it reads a set of numbers.

    `entry` moves the one bar the confirmation is swept over.
    """
    from conftest import FakeAdapter, FakeClient

    thresholds = policy_mod.Thresholds()
    if entry is not None:
        thresholds = policy_mod.Thresholds(reached_entry=entry)
    return Engine(
        FakeAdapter([home_xml]),
        FakeClient([]),
        policy_mod.Policy(thresholds=thresholds),
    )


def _recorded_decision(row):
    return Decision(
        capture_id="recorded",
        action="none",
        reached=row["reached"],
        reached_content=row["reached_content"],
        reached_entry=row["reached_entry"],
    )


def test_the_dataset_and_the_recorded_run_are_the_same_screens(recorded):
    lines = [line for line in DATASET_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == recorded["samples"] == len(recorded["readings"])
    labels = [json.loads(line)["label"] for line in lines]
    assert labels.count("destination") == recorded["destination"]
    assert labels.count("not_destination") == recorded["not_destination"]


def test_the_calibration_note_names_what_the_run_used(recorded):
    note = policy_mod.THRESHOLD_CALIBRATION
    assert "reached_calibration.jsonl" in note
    assert str(recorded["samples"]) in note
    for model in recorded["models"]:
        assert model in note


def test_the_shipped_defaults_reproduce_the_recorded_confusion_counts(recorded, home_xml):
    """The rule under test is `Engine._reached_confirmed`, not a copy of it."""
    engine = _engine(home_xml)
    thresholds = engine.policy.thresholds
    assert thresholds.reached == recorded["reached_threshold"]

    false_stops = []
    misses = []
    for row in recorded["readings"]:
        decision = _recorded_decision(row)
        confirmed = engine._reached_confirmed(decision) is True
        if row["label"] == "not_destination" and confirmed:
            false_stops.append(row)
        elif row["label"] == "destination" and not confirmed:
            misses.append(row)

    assert len(false_stops) == EXPECTED_FALSE_STOPS, [r["source"] for r in false_stops]
    assert len(misses) == EXPECTED_MISSES, [r["source"] for r in misses]
    # Every miss is the `reached` gate's doing, not the two new bars'.
    assert all(row["reached"] < thresholds.reached for row in misses)


def test_reached_content_is_out_of_the_verdict(recorded, home_xml):
    """Round 8: as a confirmation bar, content refused three screens that were
    the destination and caught nothing `reached_entry` had not already caught.
    Replacing every recorded content reading with its opposite must not move a
    single verdict."""
    engine = _engine(home_xml)
    for row in recorded["readings"]:
        decision = _recorded_decision(row)
        flipped = _recorded_decision(row)
        flipped.reached_content = round(1.0 - row["reached_content"], 4)
        assert engine._reached_confirmed(decision) == engine._reached_confirmed(flipped)


def test_no_entry_bar_on_the_recorded_grid_does_better(recorded, home_xml):
    """The confirmation is read off `reached` and `reached_entry`, so the grid
    that decides it has one axis now. 0.40 sits on its best plateau."""
    shipped = policy_mod.Thresholds().reached_entry
    counts = {}
    for entry in recorded["entry_grid"]:
        engine = _engine(home_xml, entry=entry)
        false_stops = 0
        misses = 0
        for row in recorded["readings"]:
            confirmed = engine._reached_confirmed(_recorded_decision(row)) is True
            if row["label"] == "not_destination" and confirmed:
                false_stops += 1
            elif row["label"] == "destination" and not confirmed:
                misses += 1
        counts[entry] = (false_stops, misses)

    assert counts[shipped] == (EXPECTED_FALSE_STOPS, EXPECTED_MISSES)
    assert min(counts.values()) == (EXPECTED_FALSE_STOPS, EXPECTED_MISSES)
    # The recorded two-axis sweep was taken with content in the verdict. Its
    # cell at the shipped pair holds the same two numbers, which is the whole
    # case for taking content out: on this dataset it changes nothing.
    cell = next(
        row
        for row in recorded["sweep"]
        if row["content"] == policy_mod.Thresholds().reached_content
        and row["entry"] == shipped
    )
    assert (cell["false_stops"], cell["misses"]) == (EXPECTED_FALSE_STOPS, EXPECTED_MISSES)


def test_the_entry_question_is_what_separates_the_two_groups(recorded):
    """Why there are two questions and not one: `reached_entry` leaves an
    empty band, `reached_content` on its own does not."""
    entry = recorded["margins"]["reached_entry"]
    assert entry["destination_max"] < entry["not_destination_min"]
    content = recorded["margins"]["reached_content"]
    assert content["destination_min"] < content["not_destination_max"], (
        "overlaps, which is why it only opens the gate"
    )


def test_the_fixture_carries_no_personal_data():
    """The run logs it was built from hold the owner's address, orders and
    search history. None of that may live inside the plugin.

    Only the machine-shaped leaks are checked here, because naming the real
    strings would mean writing them into the plugin - which is the thing this
    test is for. The full list lives with the scrubber that built the file,
    outside the plugin, and the build fails if any of it survives.
    """
    import re

    text = DATASET_PATH.read_text(encoding="utf-8")
    for pattern in (
        r"1[3-9]\d{9}",  # CN mobile number
        r"[\w.+-]+@[\w-]+\.[\w.]+",  # email address
        r"/Users/",  # a path off somebody's machine
        r"\bsk-",  # an API key
    ):
        assert not re.search(pattern, text), pattern
