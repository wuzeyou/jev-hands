"""Data model shared by every tool.

All of these serialise to plain JSON objects; the MCP layer adds a ``summary``
string on top so a human can read the result without parsing.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Element kinds.
KIND_INTERACTIVE = "interactive"
KIND_TEXT_LEAF = "text_leaf"
KIND_CONTAINER = "container"

# Confidence tiers for a decision.
TIER_ACT = "act"
TIER_ASK = "ask"
TIER_STOP = "stop"

# How a one-off yes/no probability from `jev_check` is reported. The raw
# probability goes back as well; this is only the reading of it.
TIER_YES = "yes"
TIER_UNSURE = "unsure"
TIER_NO = "no"

# Actions the decision layer may pick. There is deliberately no `done` and no
# `give_up`: judging whether the whole task finished belongs to the caller.
ACTION_TAP = "tap_element"
ACTION_TYPE = "type_text"
ACTION_SCROLL_DOWN = "scroll_down"
ACTION_SCROLL_UP = "scroll_up"
ACTION_BACK = "go_back"
ACTION_NONE = "none"

MUTATING_ACTIONS = (
    ACTION_TAP,
    ACTION_TYPE,
    ACTION_SCROLL_DOWN,
    ACTION_SCROLL_UP,
    ACTION_BACK,
)

# Stop reasons, design section 8.2.
STOP_REACHED = "reached"
STOP_ASK_LOW_CONFIDENCE = "ask_low_confidence"
STOP_ASK_CONTRADICTION = "ask_contradiction"
STOP_ASK_POPUP = "ask_popup"
STOP_NONE = "stop_none"
STOP_INVALID_ANSWER = "invalid_answer"
STOP_UNCHANGED_TWICE = "unchanged_twice"
STOP_OBSERVE_FAILED = "observe_failed"
STOP_MAX_STEPS = "max_steps"
STOP_EXECUTE_UNCERTAIN = "execute_uncertain"
STOP_STALE_SCREEN = "stale_screen"

STOP_REASONS = (
    STOP_REACHED,
    STOP_ASK_LOW_CONFIDENCE,
    STOP_ASK_CONTRADICTION,
    STOP_ASK_POPUP,
    STOP_NONE,
    STOP_INVALID_ANSWER,
    STOP_UNCHANGED_TWICE,
    STOP_OBSERVE_FAILED,
    STOP_MAX_STEPS,
    STOP_EXECUTE_UNCERTAIN,
    STOP_STALE_SCREEN,
)

# What the caller should do about each stop reason. Kept next to the list so the
# two never drift apart.
STOP_REASON_HINTS = {
    STOP_REACHED: "The `end_state` gate opened and the confirmation backed it up: `reached` over its bar and `reached_entry` under its, with `reached_content` reported alongside. Look at the final screen before calling the task done.",
    STOP_ASK_LOW_CONFIDENCE: "Look at the candidate table and top3, then either call jev_act yourself or ask the user.",
    STOP_ASK_CONTRADICTION: "The answers disagree with each other. Read `decision.raw` and decide yourself.",

    STOP_ASK_POPUP: "A blocking popup is likely. Decide whether to dismiss it and which element to use.",
    STOP_NONE: "No listed action moves things forward. Re-plan, supply a different goal, or ask the user.",
    STOP_INVALID_ANSWER: "The model answer failed validation twice. Report it and retry later.",
    STOP_UNCHANGED_TWICE: "Two executed steps left the screen unchanged. Try a different approach.",
    STOP_OBSERVE_FAILED: "The UI tree could not be read. Take a screenshot and look at it.",
    STOP_MAX_STEPS: "Step budget spent. Call jev_run again if the goal still makes sense.",
    STOP_EXECUTE_UNCERTAIN: "The action may or may not have landed. Observe again before doing anything else.",
    STOP_STALE_SCREEN: "The screen moved on between the decision and the action, so nothing was done. Observe again and decide on the new screen.",
}

# Said instead of the `reached` hint when the gate opened, the confirmation did
# not back it up, and the model had no action left to offer. Round 6 ended that
# step as `stop_none`, whose hint sends the caller off to re-plan, on two
# screens that were the destination. The step stops on the screen either way;
# what changes is that the caller is told where it is.
STOP_REACHED_UNCONFIRMED_HINT = (
    "The `end_state` gate passed, the confirmation did not back it up "
    "(`reached` under `reached_threshold` or `reached_entry` over "
    "`reached_entry_threshold`), and the model sees no further action on this "
    "screen. This is probably the destination: verify it with jev_check or by "
    "reading the candidate table before calling the task done."
)

# Said instead of the plain `ask_contradiction` hint whenever the caller gave
# an `end_state`, whichever way the answers contradicted each other. A
# contradiction is what the model produces on a page that is finished and
# still has something tappable on it, and round 7 lost a whole run to one, so
# the three `reached` readings are reported alongside the stop and the hint
# quotes them. Batch G reported them for one kind of contradiction only, and
# rounds 7 and 8 saw four on a phone, none of them that kind.
STOP_CONTRADICTION_AT_END_STATE_HINT = (
    "The answers contradict each other on a step that had an `end_state`, "
    "which is what a screen with nothing left to do on it often produces. "
    "The `end_state` readings are on the step (`reached_candidate`, "
    "`reached_content`, `reached_entry`) and `contradiction_kind` says which "
    "contradiction fired."
)


# How the three readings are named inside the `ask_contradiction` hint, where
# the sentence has to stay short. The keys are the field names on the step.
CONTRADICTION_HINT_LABELS = {
    "reached": "reached",
    "reached_content": "content",
    "reached_entry": "entry",
}


def stop_hint(
    stop_reason: Optional[str],
    reached_confirmed: Optional[bool] = None,
    *,
    contradiction_at_end_state: bool = False,
    readings: Optional[Dict[str, Optional[float]]] = None,
) -> Optional[str]:
    """What to tell the caller about one stop.

    `reached` has two of them: one for a confirmed destination and one for a
    stop the confirmation did not back up. `ask_contradiction` has its own
    second one for a step that was given an `end_state`, and that one quotes
    the three readings so nobody has to go digging.
    """
    if stop_reason == STOP_REACHED and reached_confirmed is False:
        return STOP_REACHED_UNCONFIRMED_HINT
    if stop_reason == STOP_ASK_CONTRADICTION and contradiction_at_end_state:
        quoted = ", ".join(
            f"{CONTRADICTION_HINT_LABELS.get(name, name)} "
            f"{'-' if value is None else format(value, '.2f')}"
            for name, value in (readings or {}).items()
        )
        return (
            f"{STOP_CONTRADICTION_AT_END_STATE_HINT} This step read {quoted}; "
            "verify with jev_check before re-planning."
            if quoted
            else STOP_CONTRADICTION_AT_END_STATE_HINT
        )
    return STOP_REASON_HINTS.get(stop_reason or "", None)

# Why an observation produced nothing anyone can act on. Reported as `reason`
# next to an `observe_failed` error, because the three cases need three
# different things from the caller.
OBSERVE_DUMP_ERROR = "dump_error"
OBSERVE_EMPTY_TREE = "empty_tree"
OBSERVE_HIDDEN_TREE = "hidden_tree"

OBSERVE_FAILED_REASONS = (
    OBSERVE_DUMP_ERROR,
    OBSERVE_EMPTY_TREE,
    OBSERVE_HIDDEN_TREE,
)

OBSERVE_FAILED_SUMMARIES = {
    OBSERVE_DUMP_ERROR: "The UI tree could not be read at all.",
    OBSERVE_EMPTY_TREE: "The UI tree came back with no nodes in it.",
    OBSERVE_HIDDEN_TREE: (
        "The window in front is there but came back with no readable content in it."
    ),
}

OBSERVE_FAILED_HINTS = {
    OBSERVE_DUMP_ERROR: (
        "Check that the phone is awake and still connected, then observe again. "
        "/jev-hands:doctor re-initialises the agent on the phone."
    ),
    OBSERVE_EMPTY_TREE: "Call jev_observe(screenshot=true) and look at the picture.",
    OBSERVE_HIDDEN_TREE: (
        "This is most likely a secure window - a biometric prompt, a system password "
        "or PIN entry, a payment keyboard - or an app that switches its accessibility "
        "tree off. Android hides those from accessibility, so this plugin can neither "
        "read nor act on this screen, and a screenshot of it is often black. Hand over "
        "to the person holding the phone and carry on from whatever is on screen "
        "afterwards."
    ),
}


def observe_failed_result(
    reason: str, *, error: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """The `observe_failed` payload for one reason.

    A `dump_error` keeps the adapter's own summary and hint, which say what went
    wrong with the read itself; the two screen-shaped reasons take theirs from
    the tables above.
    """
    out: Dict[str, Any] = dict(error or {})
    out["ok"] = False
    out["error"] = STOP_OBSERVE_FAILED
    out["reason"] = reason
    out.setdefault("summary", OBSERVE_FAILED_SUMMARIES[reason])
    out.setdefault("hint", OBSERVE_FAILED_HINTS[reason])
    return out


def new_capture_id() -> str:
    return "c_" + uuid.uuid4().hex[:16]


@dataclass
class Element:
    """One row of the candidate table handed to the model.

    `window` is the package of the top-level window this element was drawn in,
    shortened to its last two dotted segments. A screen can carry windows from
    several packages at once - a permission dialog over an app, a vendor
    chooser - and every one of them that is not decorative contributes rows.
    """

    index: int
    name: str
    id: Optional[str]
    type: str
    at: Tuple[int, int]
    bounds: Tuple[int, int, int, int]
    editable: bool = False
    scrollable: bool = False
    focused: bool = False
    checked: Optional[bool] = None
    kind: str = KIND_INTERACTIVE
    window: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "id": self.id,
            "type": self.type,
            "at": list(self.at),
            "bounds": list(self.bounds),
            "editable": self.editable,
            "scrollable": self.scrollable,
            "focused": self.focused,
            "checked": self.checked,
            "kind": self.kind,
            "window": self.window,
        }

    def to_state_row(self, *, with_window: bool = False) -> Dict[str, Any]:
        """Compact form that goes into the Jev `state`. Coordinates are left out
        on purpose: the model must never see or produce them.

        `with_window` is set only when the screen holds windows from more than
        one package; otherwise the column would repeat one value per row.
        """
        row: Dict[str, Any] = {"index": self.index, "name": self.name, "kind": self.kind}
        if self.id:
            row["id"] = self.id
        if self.editable:
            row["editable"] = True
        if self.scrollable:
            row["scrollable"] = True
        if self.focused:
            row["focused"] = True
        if self.checked is not None:
            row["checked"] = self.checked
        if with_window and self.window:
            row["window"] = self.window
        return row


@dataclass
class Screen:
    """One observation.

    `app` says what is in front: `package` and `activity` of the foreground app,
    plus `windows`, every package drawing a non-decorative top-level window,
    topmost last. More than one entry means something is sitting over the app -
    a permission dialog, a chooser - and its buttons are in `elements` too.

    `unreadable` is set only when the table came out empty, and then it says
    which kind of nothing this is: `empty_tree` or `hidden_tree`.
    """

    capture_id: str
    captured_at: float
    app: Dict[str, Optional[str]]
    elements: List[Element]
    fingerprint: str
    truncated: Optional[Dict[str, int]] = None
    scroll: Dict[str, bool] = field(default_factory=lambda: {"can_down": False, "can_up": False})
    size: Optional[Tuple[int, int]] = None
    screenshot_path: Optional[str] = None
    note: Optional[str] = None
    unreadable: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "capture_id": self.capture_id,
            "captured_at": round(self.captured_at, 3),
            "app": self.app,
            "elements": [e.to_json() for e in self.elements],
            "truncated": self.truncated,
            "fingerprint": self.fingerprint,
            "scroll": self.scroll,
        }
        if self.size:
            out["size"] = list(self.size)
        if self.screenshot_path:
            out["screenshot_path"] = self.screenshot_path
        if self.note:
            out["note"] = self.note
        if self.unreadable:
            out["unreadable"] = self.unreadable
        return out

    def element_by_index(self, index: int) -> Optional[Element]:
        for element in self.elements:
            if element.index == index:
                return element
        return None


@dataclass
class Decision:
    """One Jev decision that has not been executed yet."""

    capture_id: str
    action: str
    target: Optional[int] = None
    target_name: Optional[str] = None
    text_to_type: Optional[str] = None
    confidence: float = 0.0
    tier: str = TIER_STOP
    top3: List[List[Any]] = field(default_factory=list)
    popup: Optional[float] = None
    reached: Optional[float] = None
    # The two literal confirmations, answered on every step that has an
    # `end_state`, in the same request. `reached_content` asks whether the
    # content is on the screen, `reached_entry` whether the screen only leads
    # to it; the loop combines them with `reached`.
    reached_content: Optional[float] = None
    reached_entry: Optional[float] = None
    flags: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
    usage: Dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    model: Optional[str] = None
    consumed: bool = False

    def to_json(self) -> Dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "action": self.action,
            "target": self.target,
            "target_name": self.target_name,
            "text_to_type": self.text_to_type,
            "confidence": round(self.confidence, 4),
            "tier": self.tier,
            "top3": self.top3,
            "popup": self.popup,
            "reached": self.reached,
            "reached_content": self.reached_content,
            "reached_entry": self.reached_entry,
            "flags": self.flags,
            "raw": self.raw,
            "usage": self.usage,
            "latency_ms": self.latency_ms,
            "model": self.model,
            "consumed": self.consumed,
        }


@dataclass
class StepResult:
    """One full observe -> decide -> execute -> observe cycle.

    `reached_candidate` says the `end_state` gate opened on this step,
    `reached_content` and `reached_entry` are what the two confirmation
    questions in the same request answered about the same screen, and
    `reached_confirmed` is the verdict: True means `reached` was over its bar
    and `reached_entry` under its, False means one of the two said otherwise,
    None means there was nothing to read. `reached_content` is reported but
    does not enter that verdict - it is what opens the gate. A step can carry
    all of them and still have executed an action: the gate is cheap and the
    confirmation is what stops a run.

    A step that stops as `reached` with `reached_confirmed: false` is the one
    case where gate and confirmation disagree and there was nothing left to do
    - probably the destination, not confirmed. Its hint says so.

    `rerendered` says the step threw away its first look: the candidate table
    was far too small for the screen the run had just come from, so the step
    waited once, read the tree again and decided on the second look.

    `unstable` says the screen read after the action did not hold still, so it
    is not handed to the next step as a starting point.
    """

    before: Optional[Screen] = None
    decision: Optional[Decision] = None
    executed: bool = False
    after: Optional[Screen] = None
    changed: Optional[bool] = None
    stop_reason: Optional[str] = None
    error: Optional[Dict[str, Any]] = None
    timings: Dict[str, Any] = field(default_factory=dict)
    screenshot_path: Optional[str] = None
    reached_candidate: bool = False
    reached_content: Optional[float] = None
    reached_entry: Optional[float] = None
    reached_confirmed: Optional[bool] = None
    # True when this step stopped on a contradiction and the caller had given
    # an `end_state`; it only picks the hint.
    contradiction_at_end_state: bool = False
    # Which of the contradictions in `policy.CONTRADICTION_KINDS` fired, so a
    # caller can tell them apart without matching on the hint text.
    contradiction_kind: Optional[str] = None
    unstable: bool = False
    rerendered: bool = False

    def reached_readings(self) -> Optional[Dict[str, Optional[float]]]:
        """The three `end_state` numbers this step read, or None when it read none."""
        if self.decision is None or self.decision.reached is None:
            return None
        return {
            "reached": self.decision.reached,
            "reached_content": self.decision.reached_content,
            "reached_entry": self.decision.reached_entry,
        }

    def to_json(self) -> Dict[str, Any]:
        return {
            "before": self.before.to_json() if self.before else None,
            "decision": self.decision.to_json() if self.decision else None,
            "executed": self.executed,
            "after": self.after.to_json() if self.after else None,
            "changed": self.changed,
            "stop_reason": self.stop_reason,
            "stop_hint": stop_hint(
                self.stop_reason,
                self.reached_confirmed,
                contradiction_at_end_state=self.contradiction_at_end_state,
                readings=self.reached_readings(),
            ),
            "error": self.error,
            "timings": self.timings,
            "screenshot_path": self.screenshot_path,
            "reached_candidate": self.reached_candidate,
            "reached_content": self.reached_content,
            "reached_entry": self.reached_entry,
            "reached_confirmed": self.reached_confirmed,
            "contradiction_kind": self.contradiction_kind,
            "unstable": self.unstable,
            "rerendered": self.rerendered,
        }


@dataclass
class RunResult:
    """A short loop of steps plus why it ended."""

    goal: str
    steps: List[StepResult] = field(default_factory=list)
    stop_reason: Optional[str] = None
    final: Optional[Screen] = None

    @property
    def stale_count(self) -> int:
        """Steps that decided on a screen which had moved on before the action."""
        return sum(1 for step in self.steps if step.stop_reason == STOP_STALE_SCREEN)

    @property
    def reused_count(self) -> int:
        """Steps that started from the screen the previous step had read."""
        return sum(1 for step in self.steps if step.timings.get("reused_observation"))

    @property
    def gate_hits(self) -> int:
        """Steps where the `end_state` gate opened and the confirmations were read."""
        return sum(1 for step in self.steps if step.reached_candidate)

    @property
    def confirmed(self) -> int:
        """Gate hits the two confirmation questions backed up."""
        return sum(1 for step in self.steps if step.reached_confirmed is True)

    @property
    def unconfirmed_stops(self) -> int:
        """Steps that stopped as `reached` although the confirmations did not."""
        return sum(
            1
            for step in self.steps
            if step.stop_reason == STOP_REACHED and step.reached_confirmed is False
        )

    @property
    def final_reached_confirmed(self) -> Optional[bool]:
        """How the last step read its confirmations, which is what the run's
        own stop reason was decided on."""
        return self.steps[-1].reached_confirmed if self.steps else None

    def to_json(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "steps": [s.to_json() for s in self.steps],
            "step_count": len(self.steps),
            # Counted here so a measurement round can read them straight off the
            # result instead of walking the steps.
            "stale_count": self.stale_count,
            "reused_count": self.reused_count,
            "gate_hits": self.gate_hits,
            "confirmed": self.confirmed,
            "unconfirmed_stops": self.unconfirmed_stops,
            "stop_reason": self.stop_reason,
            "stop_hint": stop_hint(
                self.stop_reason,
                self.final_reached_confirmed,
                contradiction_at_end_state=bool(
                    self.steps and self.steps[-1].contradiction_at_end_state
                ),
                readings=self.steps[-1].reached_readings() if self.steps else None,
            ),
            "final": self.final.to_json() if self.final else None,
        }


def now() -> float:
    return time.time()
