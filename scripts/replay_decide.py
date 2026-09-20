#!/usr/bin/env python3
"""Replay recorded screens against the real Jev API and score the answers.

This is the tool for recalibrating after the model moves: run it, compare the
accuracy and the confidence distribution with the last recorded run, and adjust
the thresholds if they drifted.

Ground truth is written as element *names*, never indices: the candidate builder
renumbers rows whenever its rules change, so an index-based table would rot.

Usage:
    uv run python scripts/replay_decide.py            # every case once
    uv run python scripts/replay_decide.py --repeat 3 # sample each case 3 times
    uv run python scripts/replay_decide.py --case home_delivery

The API key is read the same way the server reads it and only ever travels in
the Authorization header. It is never printed.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402
import json  # noqa: E402
import statistics  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import List, Optional, Sequence  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jev_hands import credentials  # noqa: E402
from jev_hands.core import candidates as candidates_mod  # noqa: E402
from jev_hands.core import policy as policy_mod  # noqa: E402
from jev_hands.core import questions as questions_mod  # noqa: E402
from jev_hands.core import validate as validate_mod  # noqa: E402
from jev_hands.core.jev_client import JevClient  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"

HOME = "delivery_home.xml"
SETTINGS = "settings_home.xml"
SEARCH = "delivery_search.xml"
FOOD_LIST = "delivery_food_list.xml"
PROFILE = "delivery_profile.xml"
CLONE = "popup_app_clone_chooser.xml"
CAPTCHA = "popup_captcha_text.xml"
U2_HOME = "u2_delivery_home_with_systemui.xml"
U2_RESULT = "u2_delivery_search_result.xml"


@dataclass
class Case:
    """One recorded screen plus one goal plus the answer a person would give."""

    key: str
    fixture: str
    goal: str
    expect: Sequence[str]
    text_to_type: Optional[str] = None
    end_state: Optional[str] = None
    expect_action: Optional[str] = None
    source: str = ""


CASES: List[Case] = [
    # Round one, delivery app home, seven goals. The recorded score was 6/7.
    Case("home_delivery", HOME, "打开外卖频道", ["外卖#83c2"], source="round1 A"),
    Case("home_search", HOME, "搜索奶茶", ["搜索#search_layout_area"], source="round1 A + round2 fix"),
    Case("home_orders", HOME, "查看我的订单", ["我的"], source="round1 A"),
    Case("home_food_group", HOME, "打开美食团购", ["团购#8fc8", "美食"], source="round1 A"),
    Case("home_cart", HOME, "查看购物车", ["购物车"], source="round1 A"),
    Case("home_profile", HOME, "进入个人中心", ["我的"], source="round1 A"),
    Case("home_steps", HOME, "查看昨天的步数", ["none"], source="round1 A (negative)"),
    # Round one, settings.
    Case("settings_bluetooth", SETTINGS, "打开蓝牙设置", ["蓝牙, 已开启"], source="round1 A"),
    Case("settings_wlan", SETTINGS, "连接WLAN", ["WLAN, HPWZ413_5G"], source="round1 A"),
    Case("settings_battery", SETTINGS, "查看电池用量", ["none"], expect_action="scroll_down", source="round1 A (needs a scroll)"),
    # Round one, search page.
    Case("search_type", SEARCH, "在搜索框输入奶茶", ["汉堡店"], text_to_type="奶茶", expect_action="type_text", source="round1 A"),
    Case("search_clear_history", SEARCH, "清空搜索历史", ["清除历史记录"], source="round1 A"),
    # Round one, delivery list.
    Case("food_orders", FOOD_LIST, "查看我的订单", ["订单"], source="round1 A"),
    Case("food_food", FOOD_LIST, "打开美食分类", ["美食"], source="round1 A"),
    Case("food_cart", FOOD_LIST, "查看购物车", ["购物车"], source="round1 A"),
    # Round two, the page that used to collapse to five bottom tabs.
    Case("profile_orders", PROFILE, "查看我的订单", ["订单", "全部订单"], source="round2 T4 counter-experiment"),
    # Round one, popups.
    Case("clone_chooser", CLONE, "打开示例外卖", ["示例"], source="round1 E (was wrong)"),
    Case("captcha", CAPTCHA, "打开外卖频道", ["none"], source="round1 E"),
    # Round three, uiautomator2 trees.
    Case("u2_home_delivery", U2_HOME, "打开外卖频道", ["外卖#83c2"], source="round3 q1"),
    Case("u2_result_shop", U2_RESULT, "打开快餐店二街店", ["快餐店(二街店) 4.0 店内提供fastfood早餐"], source="round3 q2"),
]


@dataclass
class Outcome:
    case: Case
    chosen: str = ""
    action: str = ""
    confidence: float = 0.0
    tier: str = ""
    popup: Optional[float] = None
    correct: bool = False
    latency_ms: int = 0
    input_tokens: int = 0
    model: str = ""
    invalid: List[str] = field(default_factory=list)


def run_case(client: JevClient, case: Case, policy: policy_mod.Policy) -> Outcome:
    screen = candidates_mod.build_screen(
        (FIXTURES / case.fixture).read_text(encoding="utf-8"),
        token_budget=policy.token_budget,
        scroll={"can_down": True, "can_up": False},
    )
    question_set = questions_mod.build_questions(
        screen, text_to_type=case.text_to_type, end_state=case.end_state
    )
    state = questions_mod.build_state(
        candidates_mod.screen_state(screen),
        goal=case.goal,
        app=screen.app,
        end_state=case.end_state,
        text_to_type=case.text_to_type,
    )
    response = client.evaluate(state, question_set, model=policy.model)
    failures = validate_mod.validate_answers(
        response.answers,
        questions_mod.option_sets(question_set),
        answer_capture_id=screen.capture_id,
        current_capture_id=screen.capture_id,
    )

    action_answer = response.answers.get(questions_mod.Q_ACTION, {})
    action = action_answer.get("choice", "")
    target_question = (
        questions_mod.Q_TYPE_TARGET
        if action == "type_text" and questions_mod.Q_TYPE_TARGET in response.answers
        else questions_mod.Q_TAP_TARGET
    )
    target_answer = response.answers.get(target_question, {})
    choice = target_answer.get("choice", "none")
    confidence = float(target_answer.get("confidence") or 0.0)

    if choice == "none":
        chosen = "none"
    else:
        element = screen.element_by_index(int(choice))
        chosen = element.name if element else f"?{choice}"

    correct = chosen in case.expect
    if case.expect_action and action != case.expect_action:
        correct = False

    tier = policy_mod.tier_for(
        min(float(action_answer.get("confidence") or 0.0), confidence),
        policy,
    )
    popup = response.answers.get(questions_mod.Q_BLOCKING_POPUP, {}).get("noul")

    return Outcome(
        case=case,
        chosen=chosen,
        action=action,
        confidence=confidence,
        tier=tier,
        popup=float(popup) if isinstance(popup, (int, float)) else None,
        correct=correct,
        latency_ms=response.latency_ms,
        input_tokens=int(response.usage.get("input_tokens") or 0),
        model=response.model or "",
        invalid=[f.check for f in failures],
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=1, help="samples per case")
    parser.add_argument("--case", action="append", help="run only these case keys")
    parser.add_argument("--model", default=None, help="override the model name")
    parser.add_argument("--json", type=Path, default=None, help="also write the raw outcomes here")
    args = parser.parse_args(argv)

    key = credentials.get()
    if not key:
        print("No API key configured. Run /jev-hands:setup or set TYPESAFE_API_KEY.", file=sys.stderr)
        return 2

    policy = policy_mod.Policy()
    if args.model:
        policy.model = args.model
    client = JevClient(key)

    selected = [c for c in CASES if not args.case or c.key in args.case]
    outcomes: List[Outcome] = []
    for case in selected:
        for _ in range(args.repeat):
            outcomes.append(run_case(client, case, policy))
    client.close()

    width = max(len(o.case.key) for o in outcomes) + 2
    print(f"{'case':<{width}}{'expected':<34}{'chosen':<34}{'conf':>6}  {'tier':<5} ok")
    for outcome in outcomes:
        expected = " | ".join(outcome.case.expect)
        print(
            f"{outcome.case.key:<{width}}{expected[:32]:<34}{outcome.chosen[:32]:<34}"
            f"{outcome.confidence:>6.2f}  {outcome.tier:<5} {'Y' if outcome.correct else 'N'}"
        )

    correct = sum(1 for o in outcomes if o.correct)
    confidences = [o.confidence for o in outcomes]
    latencies = [o.latency_ms for o in outcomes]
    tokens = [o.input_tokens for o in outcomes]
    tiers = {}
    for outcome in outcomes:
        tiers[outcome.tier] = tiers.get(outcome.tier, 0) + 1
    models = sorted({o.model for o in outcomes})
    invalid = [o for o in outcomes if o.invalid]

    print()
    print(f"accuracy        {correct}/{len(outcomes)} ({100.0 * correct / len(outcomes):.0f}%)")
    print(f"confidence      mean {statistics.fmean(confidences):.3f}  median {statistics.median(confidences):.3f}"
          f"  min {min(confidences):.2f}  max {max(confidences):.2f}")
    print(f"tiers           {tiers}")
    print(f"latency ms      mean {statistics.fmean(latencies):.0f}  median {statistics.median(latencies):.0f}"
          f"  min {min(latencies)}  max {max(latencies)}")
    print(f"input tokens    total {sum(tokens)}  mean {statistics.fmean(tokens):.0f}")
    print(f"model           {', '.join(models)}")
    print(f"invalid answers {len(invalid)}")
    drift = [m for m in models if m != policy.calibrated_model]
    if drift:
        print(f"model_drift     thresholds were calibrated on {policy.calibrated_model}; got {drift}")

    if args.json:
        args.json.write_text(
            json.dumps(
                [
                    {
                        "case": o.case.key,
                        "fixture": o.case.fixture,
                        "goal": o.case.goal,
                        "source": o.case.source,
                        "expected": list(o.case.expect),
                        "expected_action": o.case.expect_action,
                        "chosen": o.chosen,
                        "action": o.action,
                        "confidence": o.confidence,
                        "tier": o.tier,
                        "popup": o.popup,
                        "correct": o.correct,
                        "latency_ms": o.latency_ms,
                        "input_tokens": o.input_tokens,
                        "model": o.model,
                        "invalid": o.invalid,
                    }
                    for o in outcomes
                ],
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
