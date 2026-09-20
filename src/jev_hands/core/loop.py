"""The observe -> decide -> validate -> execute -> observe state machine.

Rules that are easy to break and expensive to get wrong:

* A decision is marked consumed before the action is carried out, so a failure
  in the middle can never be replayed.
* Writes are never retried. When the adapter cannot tell whether the action
  landed, the step ends with ``execute_uncertain`` and the caller observes again.
* Every exit maps onto one of the stop reasons in ``models.STOP_REASONS``.
* ``reached`` stops nothing by itself. It is a gate, and it is not the only
  way in: ``reached_content`` over its own bar opens it too, so a low
  ``reached`` alone can no longer hide a destination. What a stop is read off
  is ``reached`` against its bar and ``reached_entry``, the literal question
  riding in the same request, against its. ``reached_content`` opens the gate
  and stays out of the verdict; see ``_reached_confirmed``.
* A gate the confirmation does not back up never ends a run as ``stop_none``.
  With nothing left to do, it stops as ``reached`` with
  ``reached_confirmed: false``; with something to do, it does it.
* ``ask_contradiction`` still comes first, but a step that was given an
  ``end_state`` carries the ``end_state`` readings whichever way the answers
  contradicted each other, and names the kind that fired, instead of handing
  the caller a stop with nothing to look at.
* A candidate table far too small for the screen the run has just come from is
  a frame the app has not finished drawing, and no reading taken off it ends a
  run. The step waits once, reads the tree again and decides on the second
  look; see ``_half_drawn``.
* The screen read after an action is handed to the next step as it is. A second
  read that has to agree with the first is available behind
  ``settle_confirm_seconds`` and is off by default; see ``_settle_and_observe``.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..errors import JevHandsError
from . import candidates as candidates_mod
from . import policy as policy_mod
from . import questions as questions_mod
from . import validate as validate_mod
from .jev_client import JevClient
from .models import (
    ACTION_NONE,
    ACTION_SCROLL_DOWN,
    ACTION_SCROLL_UP,
    ACTION_TAP,
    ACTION_TYPE,
    OBSERVE_DUMP_ERROR,
    OBSERVE_EMPTY_TREE,
    STOP_ASK_CONTRADICTION,
    STOP_ASK_LOW_CONFIDENCE,
    STOP_ASK_POPUP,
    STOP_EXECUTE_UNCERTAIN,
    STOP_INVALID_ANSWER,
    STOP_MAX_STEPS,
    STOP_NONE,
    STOP_OBSERVE_FAILED,
    STOP_REACHED,
    STOP_STALE_SCREEN,
    STOP_UNCHANGED_TWICE,
    TIER_ACT,
    TIER_ASK,
    TIER_STOP,
    Decision,
    RunResult,
    Screen,
    StepResult,
    observe_failed_result,
)
from .policy import Policy

LOG = logging.getLogger(__name__)

# Stop reasons where a picture is the only way anyone can make progress: the
# tree is unreadable, or something is covering the screen that the tree does not
# describe well. Taken automatically.
SCREENSHOT_ALWAYS = (STOP_ASK_POPUP, STOP_OBSERVE_FAILED)
# Worth a picture when the caller asks for one, not by default: the candidate
# table usually tells Claude enough to choose.
SCREENSHOT_ON_REQUEST = (STOP_ASK_LOW_CONFIDENCE,)

# When a candidate table is too small to read an arrival off. Round 8 caught
# the delivery app results page one frame early twice: the tree held a back button
# and the query text and nothing else, `reached_entry` got it right both times
# and `reached_content` got it wrong both times, which is what a screen that
# has not finished drawing does to a question about content. A table this
# small, on a step whose previous screen in the same run was this much bigger,
# is looked at again before it is allowed to end anything.
HALF_DRAWN_MAX_ROWS = 3
HALF_DRAWN_PREVIOUS_ROWS = 8


class Engine:
    """Holds one adapter plus the per-run memory the model needs."""

    def __init__(
        self,
        adapter: Any,
        client: Optional[JevClient],
        policy: Optional[Policy] = None,
        *,
        screenshot_dir: Optional[Path] = None,
    ) -> None:
        self.adapter = adapter
        self.client = client
        self.policy = policy or Policy()
        self.screenshot_dir = screenshot_dir
        self.recent_actions: List[str] = []
        self._can_up = False
        self._exhausted_scrolls: set = set()
        self._unchanged_streak = 0
        self._screens: Dict[str, Screen] = {}
        self._latest_capture_id: Optional[str] = None
        # How many rows the previous step of the current run decided on. Reset
        # by `run`, because "the screen before this one" only means anything
        # inside one run.
        self._previous_rows: Optional[int] = None

    # -- observation -----------------------------------------------------------
    def scroll_hint(self) -> Dict[str, bool]:
        """Only offer a scroll direction that can still do something.

        A fresh page is assumed to be at the top, so scrolling up is not offered
        until a scroll down has actually moved something. A direction that left
        the screen unchanged is withdrawn until the screen changes again.
        """
        return {
            "can_down": ACTION_SCROLL_DOWN not in self._exhausted_scrolls,
            "can_up": self._can_up and ACTION_SCROLL_UP not in self._exhausted_scrolls,
        }

    def observe(self, *, screenshot: bool = False) -> Screen:
        screen = self.adapter.observe(
            token_budget=self.policy.token_budget,
            scroll=self.scroll_hint(),
            screenshot=screenshot,
            **({"screenshot_dir": self.screenshot_dir} if screenshot else {}),
        )
        self._screens[screen.capture_id] = screen
        self._latest_capture_id = screen.capture_id
        return screen

    def screen_for(self, capture_id: str) -> Optional[Screen]:
        return self._screens.get(capture_id)

    # -- decision --------------------------------------------------------------
    def decide(
        self,
        screen: Screen,
        *,
        goal: str,
        context: Optional[str] = None,
        text_to_type: Optional[str] = None,
        end_state: Optional[str] = None,
    ) -> Decision:
        if self.client is None:
            raise JevHandsError(
                "No TypeSafe API key is configured.",
                code="api_key_missing",
                hint="Run /jev-hands:setup to store the key, then reconnect the server in /mcp.",
            )

        question_set = questions_mod.build_questions(
            screen, text_to_type=text_to_type, end_state=end_state
        )
        problems = questions_mod.check_question_set(question_set)
        if problems:  # pragma: no cover - guards a coding mistake, not input
            raise JevHandsError(
                "The question set violates its own rules: " + "; ".join(problems),
                code="internal_error",
            )

        state = questions_mod.build_state(
            candidates_mod.screen_state(screen),
            goal=goal,
            app=screen.app,
            context=context,
            end_state=end_state,
            text_to_type=text_to_type,
            recent_actions=self.recent_actions,
        )

        response = self.client.evaluate(state, question_set, model=self.policy.model)
        options = questions_mod.option_sets(question_set)
        # Check 6 compares the observation the answer was built from against the
        # newest one this engine has taken. Passing `screen.capture_id` on both
        # sides would make the check vacuous; this way, deciding on a Screen the
        # caller has been holding while the engine moved on is caught here, and
        # a screen that changes underneath us between decision and action is
        # caught by the freshness gate in `_execute_step`.
        failures = validate_mod.validate_answers(
            response.answers,
            options,
            answer_capture_id=screen.capture_id,
            current_capture_id=self._latest_capture_id or screen.capture_id,
        )

        decision = self._decision_from(
            screen,
            response,
            failures,
            text_to_type=text_to_type,
            end_state=end_state,
        )
        return decision

    def _decision_from(
        self,
        screen: Screen,
        response: Any,
        failures: List[validate_mod.Failure],
        *,
        text_to_type: Optional[str],
        end_state: Optional[str],
    ) -> Decision:
        answers = response.answers
        action_answer = answers.get(questions_mod.Q_ACTION, {}) or {}
        tap_answer = answers.get(questions_mod.Q_TAP_TARGET, {}) or {}
        type_answer = answers.get(questions_mod.Q_TYPE_TARGET, {}) or {}
        popup_answer = answers.get(questions_mod.Q_BLOCKING_POPUP, {}) or {}
        reached_answer = answers.get(questions_mod.Q_REACHED, {}) or {}
        content_answer = answers.get(questions_mod.Q_REACHED_CONTENT, {}) or {}
        entry_answer = answers.get(questions_mod.Q_REACHED_ENTRY, {}) or {}

        action = action_answer.get("choice") or ACTION_NONE
        action_confidence = float(action_answer.get("confidence") or 0.0)
        action_probabilities = {
            k: float(v) for k, v in (action_answer.get("probabilities") or {}).items()
        }
        tap_probabilities = {
            k: float(v) for k, v in (tap_answer.get("probabilities") or {}).items()
        }
        type_probabilities = {
            k: float(v) for k, v in (type_answer.get("probabilities") or {}).items()
        }

        flags: List[str] = []
        target: Optional[int] = None
        target_name: Optional[str] = None
        confidence = action_confidence
        distribution = action_probabilities

        if action == ACTION_TAP:
            choice = tap_answer.get("choice")
            if choice not in (None, "none"):
                try:
                    target = int(choice)
                except (TypeError, ValueError):
                    target = None
            distribution = tap_probabilities or action_probabilities
            target_confidence = float(tap_answer.get("confidence") or 0.0)
            confidence = min(action_confidence, target_confidence)
        elif action == ACTION_TYPE:
            choice = type_answer.get("choice")
            if choice not in (None, "none"):
                try:
                    target = int(choice)
                except (TypeError, ValueError):
                    target = None
            distribution = type_probabilities or action_probabilities
            target_confidence = float(type_answer.get("confidence") or 0.0)
            confidence = min(action_confidence, target_confidence)

        if target is not None:
            element = screen.element_by_index(target)
            target_name = element.name if element else None

        popup = popup_answer.get("noul")
        popup = float(popup) if isinstance(popup, (int, float)) else None
        reached = reached_answer.get("noul")
        reached = float(reached) if isinstance(reached, (int, float)) else None
        reached_content = content_answer.get("noul")
        reached_content = (
            float(reached_content) if isinstance(reached_content, (int, float)) else None
        )
        reached_entry = entry_answer.get("noul")
        reached_entry = (
            float(reached_entry) if isinstance(reached_entry, (int, float)) else None
        )

        if failures:
            flags.append(policy_mod.FLAG_INVALID)

        contradictions = policy_mod.cross_checks(
            action=action,
            action_confidence=action_confidence,
            tap_choice=tap_answer.get("choice"),
            tap_probabilities=tap_probabilities,
            reached=reached,
            policy=self.policy,
        )
        if contradictions:
            flags.append(policy_mod.FLAG_CONTRADICTION)

        tier = policy_mod.tier_for(confidence, self.policy)

        if policy_mod.model_drift(response.model, self.policy):
            flags.append(policy_mod.FLAG_MODEL_DRIFT)

        return Decision(
            capture_id=screen.capture_id,
            action=action,
            target=target,
            target_name=target_name,
            text_to_type=text_to_type if action == ACTION_TYPE else None,
            confidence=confidence,
            tier=tier,
            top3=policy_mod.top3(distribution),
            popup=popup,
            reached=reached,
            reached_content=reached_content,
            reached_entry=reached_entry,
            flags=flags,
            raw={
                "answers": answers,
                "validation_failures": [f.to_json() for f in failures],
                "contradictions": contradictions,
            },
            usage=response.usage,
            latency_ms=response.latency_ms,
            model=response.model,
        )

    # -- one step --------------------------------------------------------------
    def step(
        self,
        *,
        goal: str,
        context: Optional[str] = None,
        text_to_type: Optional[str] = None,
        end_state: Optional[str] = None,
        screenshot_on_stop: Optional[bool] = None,
        reuse: Optional[Screen] = None,
    ) -> StepResult:
        """One observe / decide / validate / execute / observe cycle.

        `screenshot_on_stop`: None takes a picture when the step stops on a
        blocking popup or an unreadable tree, True adds the low-confidence stop,
        False never takes one.

        `reuse`: a screen already read by the caller to start from instead of
        observing again. Only `run` passes it; see `run` for why it is safe.
        """
        result = self._step_inner(
            goal=goal,
            context=context,
            text_to_type=text_to_type,
            end_state=end_state,
            reuse=reuse,
        )
        return self._attach_screenshot(result, self._screenshot_reasons(screenshot_on_stop))

    def _step_inner(
        self,
        *,
        goal: str,
        context: Optional[str] = None,
        text_to_type: Optional[str] = None,
        end_state: Optional[str] = None,
        reuse: Optional[Screen] = None,
    ) -> StepResult:
        timings: Dict[str, Any] = {}
        started = time.monotonic()

        reused = reuse is not None and bool(reuse.elements)
        timings["reused_observation"] = reused
        if reused:
            before = reuse
        else:
            try:
                before = self.observe()
            except JevHandsError as exc:
                return StepResult(
                    stop_reason=STOP_OBSERVE_FAILED,
                    error=observe_failed_result(OBSERVE_DUMP_ERROR, error=exc.to_result()),
                )
        timings["observe_ms"] = 0 if reused else int((time.monotonic() - started) * 1000)

        if not before.elements:
            return StepResult(
                before=before,
                stop_reason=STOP_OBSERVE_FAILED,
                error=observe_failed_result(before.unreadable or OBSERVE_EMPTY_TREE),
                timings=timings,
            )

        # Read before this step overwrites it: `_half_drawn` compares the table
        # in front of it against the one the previous step decided on.
        previous_rows = self._previous_rows
        self._previous_rows = len(before.elements)

        attempts = 0
        decision: Optional[Decision] = None
        while attempts < 2:
            attempts += 1
            decide_started = time.monotonic()
            try:
                decision = self.decide(
                    before,
                    goal=goal,
                    context=context,
                    text_to_type=text_to_type,
                    end_state=end_state,
                )
            except JevHandsError as exc:
                return StepResult(before=before, error=exc.to_result(), timings=timings)
            timings["decide_ms"] = int((time.monotonic() - decide_started) * 1000)
            if policy_mod.FLAG_INVALID not in decision.flags:
                break
            LOG.warning("model answer failed validation, attempt %d", attempts)
            if attempts < 2:
                try:
                    before = self.observe()
                    timings["reused_observation"] = False
                except JevHandsError as exc:
                    return StepResult(
                        stop_reason=STOP_OBSERVE_FAILED,
                        error=observe_failed_result(
                            OBSERVE_DUMP_ERROR, error=exc.to_result()
                        ),
                    )

        assert decision is not None
        if policy_mod.FLAG_INVALID in decision.flags:
            return StepResult(
                before=before, decision=decision, stop_reason=STOP_INVALID_ANSWER, timings=timings
            )

        rerendered = False
        if self._half_drawn(before, decision, previous_rows):
            fresh = self._look_again(
                timings,
                goal=goal,
                context=context,
                text_to_type=text_to_type,
                end_state=end_state,
            )
            if fresh is not None:
                before, decision = fresh
                self._previous_rows = len(before.elements)
                rerendered = True

        stop = self._gate(decision)
        reached_candidate = False
        reached_content: Optional[float] = None
        reached_entry: Optional[float] = None
        reached_confirmed: Optional[bool] = None
        contradiction_kind = (
            policy_mod.contradiction_kind(decision.raw.get("contradictions") or [])
            if stop == STOP_ASK_CONTRADICTION
            else None
        )
        # A contradiction is checked before the gate, so round 7 got the stop
        # back with no `end_state` readings on it at all and the planner gave
        # up on the destination. Batch G reported them for one kind of
        # contradiction and rounds 7 and 8 then saw four on a phone, all of
        # them the other kind, so no kind is singled out any more: whenever
        # there is an `end_state` the readings ride along, whichever way the
        # answers disagreed. The stop reason itself stays what it was.
        contradiction_at_end_state = bool(end_state and stop == STOP_ASK_CONTRADICTION)
        if stop == STOP_REACHED or contradiction_at_end_state:
            # The two confirmations came back with the same response, so they
            # are free to read and the timing below is zero.
            reached_candidate = self._reached_gate_open(decision)
            reached_content = decision.reached_content
            reached_entry = decision.reached_entry
            timings["confirm_ms"] = 0
            reached_confirmed = self._reached_confirmed(decision)
            if stop == STOP_REACHED and reached_confirmed is False:
                if decision.action == ACTION_NONE:
                    # The gate says destination, the confirmations say no, and
                    # the model has nothing left to do here. Round 6 sent that
                    # step through the gates behind `reached` and it landed on
                    # `stop_none`, whose hint points at re-planning, twice on a
                    # screen that was the destination. Stop on the screen and
                    # say which of the readings ended the step.
                    stop = STOP_REACHED
                else:
                    # There is something to do: do it, as any other step would.
                    stop = self._gate(decision, skip_reached=True)

        if stop:
            return StepResult(
                before=before,
                decision=decision,
                stop_reason=stop,
                timings=timings,
                reached_candidate=reached_candidate,
                reached_content=reached_content,
                reached_entry=reached_entry,
                reached_confirmed=reached_confirmed,
                contradiction_at_end_state=contradiction_at_end_state,
                contradiction_kind=contradiction_kind,
                rerendered=rerendered,
            )

        step = self._execute_step(before, decision, timings)
        step.reached_candidate = reached_candidate
        step.reached_content = reached_content
        step.reached_entry = reached_entry
        step.reached_confirmed = reached_confirmed
        step.rerendered = rerendered
        return step

    def _half_drawn(
        self, before: Screen, decision: Decision, previous_rows: Optional[int]
    ) -> bool:
        """Whether this screen is too thin to read an arrival off.

        Three things have to be true: the table in front of the step is tiny,
        the screen the previous step of this run decided on was not, and the
        readings would otherwise open the gate. The first step of a run has no
        previous screen and is never treated this way, and a step without an
        `end_state` has no readings to open the gate with, so neither can
        trigger the extra wait.
        """
        if previous_rows is None or previous_rows < HALF_DRAWN_PREVIOUS_ROWS:
            return False
        if len(before.elements) > HALF_DRAWN_MAX_ROWS:
            return False
        return self._reached_gate_open(decision)

    def _look_again(
        self,
        timings: Dict[str, Any],
        *,
        goal: str,
        context: Optional[str],
        text_to_type: Optional[str],
        end_state: Optional[str],
    ) -> Optional[Tuple[Screen, Decision]]:
        """Wait once, read the screen again and decide on what came back.

        Once, never in a loop: this is called at most one time per step, and
        whatever the second look says is what the step then works from. None
        means the second look did not produce anything usable, and the step
        carries on with the screen and the decision it already had - which the
        freshness check in `_execute_step` will refuse if the screen did move.
        """
        started = time.monotonic()
        time.sleep(self.policy.settle_extra_seconds)
        try:
            fresh = self.observe()
            if not fresh.elements:
                LOG.warning("the screen was still unreadable on the second look")
                return None
            timings["reused_observation"] = False
            decision = self.decide(
                fresh,
                goal=goal,
                context=context,
                text_to_type=text_to_type,
                end_state=end_state,
            )
            if policy_mod.FLAG_INVALID in decision.flags:
                LOG.warning("the decision on the second look failed validation")
                return None
            return fresh, decision
        except JevHandsError:
            LOG.warning("the second look at a half-drawn screen failed")
            return None
        finally:
            timings["rerender_ms"] = int((time.monotonic() - started) * 1000)

    def _reached_gate_open(self, decision: Decision) -> bool:
        """Whether this step is worth reading the confirmations on.

        Two ways in, because round 7 showed one is not enough: `reached` over
        its own bar, or `reached_content` over its. T1b_run1 stopped on the
        destination with `reached` at 0.15; the content question is the one
        that can see past a reading that low.
        """
        thresholds = self.policy.thresholds
        if decision.reached is not None and decision.reached >= thresholds.reached:
            return True
        if (
            decision.reached_content is not None
            and decision.reached_content >= thresholds.reached_content
        ):
            return True
        return False

    def _reached_confirmed(self, decision: Decision) -> Optional[bool]:
        """Read `reached` and the entry question as one verdict.

        Confirmed means both agree: `reached` is over its bar and the screen
        is not merely a way to the destination. None when there was nothing to
        read.

        `reached_content` is deliberately not in here. Round 8 measured it on
        60 screens: as a confirmation bar it refused three screens that were
        the destination - two of them frames that had not finished drawing -
        and every screen it caught, `reached_entry` had already caught. It
        stays as the second way into the gate, where it earns its keep, and it
        is still reported on the step.
        """
        if decision.reached_entry is None:
            return None
        thresholds = self.policy.thresholds
        return bool(
            decision.reached is not None
            and decision.reached >= thresholds.reached
            and decision.reached_entry <= thresholds.reached_entry
        )

    def _gate(self, decision: Decision, *, skip_reached: bool = False) -> Optional[str]:
        """Everything that can stop a step before anything is executed.

        `skip_reached` is set on the second pass, after a gate that the two
        confirmations did not back up and where the model still has an action
        to offer, so that the gates behind it get their say.
        """
        if policy_mod.FLAG_CONTRADICTION in decision.flags:
            return STOP_ASK_CONTRADICTION
        if decision.popup is not None and decision.popup >= self.policy.thresholds.popup:
            return STOP_ASK_POPUP
        if not skip_reached and self._reached_gate_open(decision):
            return STOP_REACHED
        if decision.action == ACTION_NONE:
            return STOP_NONE
        if decision.action in (ACTION_TAP, ACTION_TYPE) and decision.target is None:
            return STOP_NONE
        if decision.tier == TIER_STOP:
            return STOP_NONE
        if decision.tier == TIER_ASK:
            return STOP_ASK_LOW_CONFIDENCE
        return None

    def _screenshot_reasons(self, screenshot_on_stop: Optional[bool]) -> tuple:
        if screenshot_on_stop is False:
            return ()
        if screenshot_on_stop is True:
            return SCREENSHOT_ALWAYS + SCREENSHOT_ON_REQUEST
        return SCREENSHOT_ALWAYS

    def _attach_screenshot(self, step: StepResult, wanted: tuple) -> StepResult:
        """Take one picture when the step stopped somewhere a human or Claude
        has to look. Never fatal: a missing screenshot must not lose the step."""
        if step.stop_reason not in wanted or self.screenshot_dir is None:
            return step
        shot = getattr(self.adapter, "screenshot", None)
        if shot is None:
            return step
        try:
            step.screenshot_path = shot(self.screenshot_dir)
        except Exception:  # noqa: BLE001 - a picture is a nicety, not the job
            LOG.warning("could not take a screenshot for stop reason %s", step.stop_reason)
        return step

    def _execute_step(
        self,
        before: Screen,
        decision: Decision,
        timings: Dict[str, Any],
        *,
        verify: bool = True,
    ) -> StepResult:
        element = (
            before.element_by_index(decision.target) if decision.target is not None else None
        )
        action: Dict[str, Any] = {"type": decision.action}
        if element is not None:
            action["x"], action["y"] = element.at
        if decision.action == ACTION_TYPE:
            action["text"] = decision.text_to_type or ""

        # Look once more before acting. Between the observation the decision was
        # made on and this moment the screen may have moved on by itself - an ad
        # rotating, a page finishing its load - and tapping coordinates from the
        # old screen is the one mistake that cannot be undone.
        if verify and self.policy.verify_before_act:
            verify_started = time.monotonic()
            try:
                still_current = self.adapter.fresh(before.capture_id)
            except JevHandsError:
                still_current = False
            timings["verify_ms"] = int((time.monotonic() - verify_started) * 1000)
            if not still_current:
                decision.consumed = True
                return StepResult(
                    before=before,
                    decision=decision,
                    executed=False,
                    stop_reason=STOP_STALE_SCREEN,
                    timings=timings,
                )
        elif verify and policy_mod.FLAG_UNVERIFIED_EXECUTE not in decision.flags:
            decision.flags.append(policy_mod.FLAG_UNVERIFIED_EXECUTE)

        # Consume before executing. A decision is used at most once, even when
        # the execution below blows up half way through.
        decision.consumed = True

        execute_started = time.monotonic()
        try:
            self.adapter.execute(action)
        except JevHandsError as exc:
            timings["execute_ms"] = int((time.monotonic() - execute_started) * 1000)
            after = None
            try:
                after = self.observe()
            except JevHandsError:
                after = None
            return StepResult(
                before=before,
                decision=decision,
                executed=False,
                after=after,
                stop_reason=STOP_EXECUTE_UNCERTAIN,
                error=exc.to_result(),
                timings=timings,
            )
        timings["execute_ms"] = int((time.monotonic() - execute_started) * 1000)

        try:
            after, steady = self._settle_and_observe(before, timings)
        except JevHandsError as exc:
            return StepResult(
                before=before,
                decision=decision,
                executed=True,
                stop_reason=STOP_OBSERVE_FAILED,
                error=observe_failed_result(OBSERVE_DUMP_ERROR, error=exc.to_result()),
                timings=timings,
            )

        changed = after.fingerprint != before.fingerprint
        self._record(decision, changed)

        stop = None
        if not changed:
            self._unchanged_streak += 1
            if self._unchanged_streak >= 2:
                stop = STOP_UNCHANGED_TWICE
        else:
            self._unchanged_streak = 0

        return StepResult(
            before=before,
            decision=decision,
            executed=True,
            after=after,
            changed=changed,
            stop_reason=stop,
            timings=timings,
            unstable=not steady,
        )

    def _settle_and_observe(
        self, before: Screen, timings: Dict[str, Any]
    ) -> Tuple[Screen, bool]:
        """Wait for the screen to catch up with the action, then read it.

        Returns the screen and whether it held still, which is what decides
        whether the next step may start from it instead of reading the tree
        again.

        A fixed wait long enough for the slowest page was 43% of a step on the
        round-4 measurements, and most of it bought nothing. So the wait is
        short and the screen is read twice instead:

        * the action changed something: pay `settle_confirm_seconds` and read
          again. Two matching reads mean the page has stopped drawing and the
          screen is steady. Two different reads mean it has not; the newer one
          is kept, but the next step reads for itself. This one is off by
          default, at zero: round 6 measured it on a phone and it cost about
          750 ms on 92% of the executed steps and refused 62% of the reuse, to
          save the four or five seconds of `stale_screen` retries that reuse
          had cost over ten runs.
        * the action changed nothing: the old `settle_extra_seconds` wait and a
          second read, steady on the same terms - the second read has to agree
          with the first.
        * either wait set to zero turns its second read off, and the single
          read is handed on.
        """
        settle_ms = 0
        confirm_ms = 0
        observe_ms = 0

        started = time.monotonic()
        time.sleep(self.policy.settle_min_seconds)
        settle_ms += int((time.monotonic() - started) * 1000)

        started = time.monotonic()
        after = self.observe()
        observe_ms += int((time.monotonic() - started) * 1000)

        moved = after.fingerprint != before.fingerprint
        wait = (
            self.policy.settle_confirm_seconds
            if moved
            else self.policy.settle_extra_seconds
        )
        steady = True
        if wait > 0:
            started = time.monotonic()
            time.sleep(wait)
            slept = int((time.monotonic() - started) * 1000)
            if moved:
                confirm_ms += slept
            else:
                settle_ms += slept

            first = after
            started = time.monotonic()
            try:
                after = self.observe()
                steady = after.fingerprint == first.fingerprint
            except JevHandsError:
                # The first reading is still a reading. Losing the whole step
                # because the second look failed would be worse than keeping
                # it - but it is not steady enough to hand to the next step.
                LOG.warning("the second settle observation failed; keeping the first")
                steady = False
            observe_ms += int((time.monotonic() - started) * 1000)

        timings["settle_ms"] = settle_ms
        timings["settle_confirm_ms"] = confirm_ms
        timings["observe_after_ms"] = observe_ms
        return after, steady

    def _record(self, decision: Decision, changed: bool) -> None:
        label = decision.action
        if decision.target_name:
            label = f"{decision.action} -> {decision.target_name}"
        self.recent_actions.append(
            f"{label} -> screen {'changed' if changed else 'unchanged'}"
        )
        self.recent_actions = self.recent_actions[-5:]

        if decision.action in (ACTION_SCROLL_DOWN, ACTION_SCROLL_UP):
            if changed:
                # Moving in one direction always makes room in the other one.
                # Hitting the bottom blocks scroll_down; scrolling back up has
                # to lift that block, or the page can never be scrolled down
                # again for the rest of the run.
                self._exhausted_scrolls.clear()
                if decision.action == ACTION_SCROLL_DOWN:
                    self._can_up = True
            else:
                self._exhausted_scrolls.add(decision.action)
        elif changed:
            # A tap or a back took us to a different page: the scroll history
            # of the old one says nothing about this one.
            self._exhausted_scrolls.clear()
            self._can_up = False

    # -- short loop ------------------------------------------------------------
    def run(
        self,
        *,
        goal: str,
        context: Optional[str] = None,
        text_to_type: Optional[str] = None,
        end_state: Optional[str] = None,
        max_steps: Optional[int] = None,
        screenshot_on_stop: Optional[bool] = None,
    ) -> RunResult:
        budget = max_steps or self.policy.max_steps
        result = RunResult(goal=goal)
        # The screen read at the end of one step is the screen the next step
        # would otherwise read again, a few milliseconds later: two tree reads
        # for one state. The second one is dropped and the first is carried
        # over, capture_id and all - unless a second read disagreed with it,
        # which only happens when `settle_confirm_seconds` is switched on.
        # Nothing is relaxed by this either way: the freshness check still
        # re-reads the tree immediately before acting, which is the read that
        # protects the tap, and a screen that moved on in between costs one
        # `stale_screen` step, not the run.
        carried: Optional[Screen] = None
        self._previous_rows = None
        for _ in range(budget):
            step = self.step(
                goal=goal,
                context=context,
                text_to_type=text_to_type,
                end_state=end_state,
                screenshot_on_stop=screenshot_on_stop,
                reuse=carried,
            )
            result.steps.append(step)
            result.final = step.after or step.before
            carried = None if step.unstable else step.after
            if step.stop_reason == STOP_STALE_SCREEN:
                # Nothing was executed and the screen has already moved on. The
                # next pass observes again; it costs a step, not the run.
                continue
            if step.stop_reason:
                result.stop_reason = step.stop_reason
                return result
            if step.error:
                result.stop_reason = step.error.get("error")
                return result
        # A page that never settles spends the whole budget on steps that
        # executed nothing. Saying `max_steps` there sends the caller round the
        # same loop; `stale_screen` tells them what is actually wrong, and the
        # hint for it says what to do about it.
        if result.steps and result.steps[-1].stop_reason == STOP_STALE_SCREEN:
            result.stop_reason = STOP_STALE_SCREEN
        else:
            result.stop_reason = STOP_MAX_STEPS
        return result

    # -- direct action ---------------------------------------------------------
    def act(
        self,
        *,
        action: str,
        capture_id: str,
        index: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        text: Optional[str] = None,
    ) -> StepResult:
        """Carry out an action the caller picked. No model involved."""
        before = self.screen_for(capture_id)
        if before is None:
            raise JevHandsError(
                "That capture_id is not one of the recent observations.",
                code="stale_capture_id",
                hint="Call jev_observe first and pass the capture_id it returns.",
            )
        # Freshness only matters when the action reuses a position from that
        # observation. go_back, HOME and a scroll do not touch a coordinate the
        # old screen supplied, and a page that animates the whole time would
        # otherwise leave the caller no way off it at all.
        targeted = index is not None or (x is not None and y is not None)
        if targeted and not self.adapter.fresh(capture_id):
            raise JevHandsError(
                "The screen changed since that observation, so the action was refused.",
                code="stale_capture_id",
                hint="Call jev_observe again and redo the decision on the new screen.",
            )

        payload: Dict[str, Any] = {"type": action}
        target_name = None
        if index is not None:
            element = before.element_by_index(index)
            if element is None:
                raise JevHandsError(
                    f"There is no element {index} on that screen.",
                    code="bad_index",
                    hint="Use an index from the candidate table of that capture.",
                )
            target_name = element.name
            payload["x"], payload["y"] = element.at
        elif x is not None and y is not None:
            payload["x"], payload["y"] = int(x), int(y)
        if text is not None:
            payload["text"] = text

        decision = Decision(
            capture_id=capture_id,
            action=action,
            target=index,
            target_name=target_name,
            text_to_type=text if action == ACTION_TYPE else None,
            confidence=1.0,
            tier=TIER_ACT,
            raw={"source": "caller"},
        )
        # act() has just checked freshness itself; do not read the tree twice.
        return self._execute_step(before, decision, {}, verify=False)

    # -- one off question ------------------------------------------------------
    def check(
        self,
        *,
        question: str,
        criteria: Optional[Dict[str, str]] = None,
        screen: Optional[Screen] = None,
    ) -> Dict[str, Any]:
        if self.client is None:
            raise JevHandsError(
                "No TypeSafe API key is configured.",
                code="api_key_missing",
                hint="Run /jev-hands:setup to store the key.",
            )
        target = screen or self.observe()
        payload: Dict[str, Any] = {
            "type": "noul",
            "instructions": question + " " + questions_mod.DATA_NOT_INSTRUCTIONS,
        }
        if criteria:
            payload["criteria"] = criteria
        state = questions_mod.build_state(
            candidates_mod.screen_state(target),
            goal=question,
            app=target.app,
            recent_actions=self.recent_actions,
        )
        response = self.client.evaluate(
            state, {"check": payload}, model=self.policy.model
        )
        answer = response.answers.get("check", {}) or {}
        failures = validate_mod.validate_noul("check", answer)
        probability = float(answer.get("noul") or 0.0)
        return {
            "probability": round(probability, 4),
            "tier": policy_mod.check_tier(probability, self.policy),
            "capture_id": target.capture_id,
            "model": response.model,
            "usage": response.usage,
            "latency_ms": response.latency_ms,
            "valid": not failures,
            "validation_failures": [f.to_json() for f in failures],
        }
