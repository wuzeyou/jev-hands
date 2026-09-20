#!/usr/bin/env python3
"""Measure the two `reached` confirmation bars on real, labelled screens.

`tests/fixtures/reached_calibration.jsonl` holds one labelled screen per line:
the exact state a step sent to Jev, plus whether that screen was the
destination or only a way to it. This script sends each state once, with the
three `end_state` questions the loop asks (`reached`, `reached_content`,
`reached_entry`), then sweeps the two thresholds over a grid and prints what
each pair would cost:

* a false stop  - a `not_destination` screen the rule would confirm
* a miss        - a `destination` screen the rule would not confirm

The rule swept is wider than the one that ships. `loop._reached_confirmed`
confirms when `reached` is at or over `reached_threshold` AND `reached_entry`
is at or under the entry bar; `reached_content` opens the gate and takes no
part in it, because round 8 measured it refusing real destinations and
catching nothing the entry question had not caught. The sweep keeps the
content axis to show what putting it back into the verdict would cost. The
column that describes what ships is any content bar at or under the lowest
`reached_content` a destination read - `margins` prints that number.

Usage:
    uv run python scripts/calibrate_reached.py
    uv run python scripts/calibrate_reached.py --json out.json
    uv run python scripts/calibrate_reached.py --limit 5        # a cheap dry run
    uv run python scripts/calibrate_reached.py --save-result    # refresh the fixture

`--save-result` overwrites `tests/fixtures/reached_calibration_result.json`,
which `tests/test_reached_calibration.py` checks the shipped defaults against.
Refresh it when the model moves, read the new sweep, and move the two defaults
in `policy.Thresholds` with it.

One request per sample, so the whole dataset costs as many requests as it has
lines. The API key is read the way the server reads it and only ever travels in
the Authorization header; it is never printed.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Dict, List, Optional, Sequence  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jev_hands import credentials  # noqa: E402
from jev_hands.core import policy as policy_mod  # noqa: E402
from jev_hands.core import questions as questions_mod  # noqa: E402
from jev_hands.core.jev_client import JevClient  # noqa: E402

DATASET = ROOT / "tests" / "fixtures" / "reached_calibration.jsonl"
RESULT = ROOT / "tests" / "fixtures" / "reached_calibration_result.json"

DESTINATION = "destination"
NOT_DESTINATION = "not_destination"

CONTENT_GRID = [round(0.3 + 0.05 * i, 2) for i in range(13)]  # 0.30 .. 0.90
ENTRY_GRID = [round(0.1 + 0.05 * i, 2) for i in range(13)]  # 0.10 .. 0.70


def reached_questions() -> Dict[str, Dict[str, Any]]:
    """Exactly the three questions `build_questions` adds for an `end_state`."""
    return {
        questions_mod.Q_REACHED: {
            "type": "noul",
            "instructions": questions_mod.REACHED_INSTRUCTIONS,
            "criteria": dict(questions_mod.REACHED_CRITERIA),
        },
        questions_mod.Q_REACHED_CONTENT: {
            "type": "noul",
            "instructions": questions_mod.REACHED_CONTENT_INSTRUCTIONS,
            "criteria": dict(questions_mod.REACHED_CONTENT_CRITERIA),
        },
        questions_mod.Q_REACHED_ENTRY: {
            "type": "noul",
            "instructions": questions_mod.REACHED_ENTRY_INSTRUCTIONS,
            "criteria": dict(questions_mod.REACHED_ENTRY_CRITERIA),
        },
    }


def load_samples(limit: Optional[int]) -> List[Dict[str, Any]]:
    samples = [
        json.loads(line)
        for line in DATASET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return samples[:limit] if limit else samples


def noul(answers: Dict[str, Any], key: str) -> Optional[float]:
    value = (answers.get(key) or {}).get("noul")
    return float(value) if isinstance(value, (int, float)) else None


def confirmed(
    reading: Dict[str, Optional[float]],
    *,
    reached_bar: float,
    content_bar: float,
    entry_bar: float,
) -> bool:
    """One cell of the sweep: `reached`, content and entry bars all applied.

    Wider than what ships on purpose - see the module docstring. The rule the
    loop applies is this one with `content_bar` low enough to be out of the
    way.
    """
    reached = reading.get("reached")
    content = reading.get("reached_content")
    entry = reading.get("reached_entry")
    if reached is None or content is None or entry is None:
        return False
    return reached >= reached_bar and content >= content_bar and entry <= entry_bar


def sweep(
    readings: Sequence[Dict[str, Any]], *, reached_bar: float
) -> List[Dict[str, Any]]:
    table: List[Dict[str, Any]] = []
    for content_bar in CONTENT_GRID:
        for entry_bar in ENTRY_GRID:
            false_stops = 0
            misses = 0
            for row in readings:
                ok = confirmed(
                    row,
                    reached_bar=reached_bar,
                    content_bar=content_bar,
                    entry_bar=entry_bar,
                )
                if row["label"] == NOT_DESTINATION and ok:
                    false_stops += 1
                elif row["label"] == DESTINATION and not ok:
                    misses += 1
            table.append(
                {
                    "content": content_bar,
                    "entry": entry_bar,
                    "false_stops": false_stops,
                    "misses": misses,
                }
            )
    return table


def margins(readings: Sequence[Dict[str, Any]], key: str) -> Dict[str, Any]:
    """How far apart the two label groups sit on one question."""

    def values(label: str) -> List[float]:
        return sorted(
            row[key] for row in readings if row["label"] == label and row[key] is not None
        )

    positives = values(DESTINATION)
    negatives = values(NOT_DESTINATION)
    out: Dict[str, Any] = {
        "destination_min": positives[0] if positives else None,
        "destination_max": positives[-1] if positives else None,
        "not_destination_min": negatives[0] if negatives else None,
        "not_destination_max": negatives[-1] if negatives else None,
    }
    if positives and negatives:
        # Positive when the two groups do not overlap at all: the width of the
        # empty band a threshold could sit in.
        out["gap"] = round(positives[0] - negatives[-1], 4)
    return out


def best_pairs(table: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fewest false stops first, then fewest misses; ties kept."""
    ordered = sorted(table, key=lambda row: (row["false_stops"], row["misses"]))
    top = (ordered[0]["false_stops"], ordered[0]["misses"])
    return [row for row in ordered if (row["false_stops"], row["misses"]) == top]


def print_table(table: Sequence[Dict[str, Any]]) -> None:
    by_pair = {(row["content"], row["entry"]): row for row in table}
    header = "content\\entry " + " ".join(f"{entry:>5.2f}" for entry in ENTRY_GRID)
    print(header)
    for content_bar in CONTENT_GRID:
        cells = []
        for entry_bar in ENTRY_GRID:
            row = by_pair[(content_bar, entry_bar)]
            cells.append(f"{row['false_stops']}/{row['misses']}".rjust(5))
        print(f"       {content_bar:.2f} " + " ".join(cells))
    print("cells are false_stops/misses")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=None, help="also write the raw readings here")
    parser.add_argument("--limit", type=int, default=None, help="only the first N samples")
    parser.add_argument(
        "--model", default="jev-latest", help="model id to send (default jev-latest)"
    )
    parser.add_argument(
        "--save-result",
        action="store_true",
        help=f"write the sweep table to {RESULT.relative_to(ROOT)}",
    )
    args = parser.parse_args(argv)

    api_key = credentials.get()
    if not api_key:
        print(
            "No API key. Set TYPESAFE_API_KEY or store one the way the server reads it.",
            file=sys.stderr,
        )
        return 2

    samples = load_samples(args.limit)
    if not samples:
        print(f"no samples in {DATASET}", file=sys.stderr)
        return 2

    client = JevClient(api_key)
    questions = reached_questions()
    readings: List[Dict[str, Any]] = []
    model_ids: set = set()
    try:
        for n, sample in enumerate(samples, start=1):
            response = client.evaluate(sample["state"], questions, model=args.model)
            model_ids.add(response.model)
            row = {
                "source": sample["source"],
                "screen_kind": sample.get("screen_kind"),
                "end_state": sample["end_state"],
                "label": sample["label"],
                "reached": noul(response.answers, questions_mod.Q_REACHED),
                "reached_content": noul(response.answers, questions_mod.Q_REACHED_CONTENT),
                "reached_entry": noul(response.answers, questions_mod.Q_REACHED_ENTRY),
            }
            readings.append(row)
            print(
                f"{n:3d}/{len(samples)} {row['label']:16s} {row['screen_kind'] or '':22s} "
                f"reached={row['reached']} content={row['reached_content']} "
                f"entry={row['reached_entry']}"
            )
    finally:
        client.close()

    reached_bar = policy_mod.Thresholds().reached
    table = sweep(readings, reached_bar=reached_bar)
    winners = best_pairs(table)

    positives = sum(1 for row in readings if row["label"] == DESTINATION)
    print()
    print(
        f"{len(readings)} screens: {positives} destination, "
        f"{len(readings) - positives} not_destination; model(s) "
        f"{', '.join(sorted(m or '?' for m in model_ids))}; reached bar {reached_bar}"
    )
    print()
    print_table(table)
    print()
    for key in ("reached", "reached_content", "reached_entry"):
        print(f"{key:16s} {margins(readings, key)}")
    print()
    print("best pairs (fewest false stops, then fewest misses):")
    for row in winners:
        print(
            f"  content {row['content']:.2f} entry {row['entry']:.2f} -> "
            f"{row['false_stops']} false stop(s), {row['misses']} miss(es)"
        )

    payload = {
        "dataset": str(DATASET.relative_to(ROOT)),
        "samples": len(readings),
        "destination": positives,
        "not_destination": len(readings) - positives,
        "models": sorted(m or "?" for m in model_ids),
        "reached_threshold": reached_bar,
        "content_grid": CONTENT_GRID,
        "entry_grid": ENTRY_GRID,
        "readings": readings,
        "sweep": table,
        "best_pairs": winners,
        "margins": {key: margins(readings, key) for key in
                    ("reached", "reached_content", "reached_entry")},
    }
    if args.json:
        args.json.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nraw readings -> {args.json}")
    if args.save_result:
        RESULT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"sweep table -> {RESULT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
