"""The jev-hands Android MCP server.

Nine tools, every one of them returning a JSON object with a ``summary`` string.
The server starts even when no API key is stored: the tools that need one say so
and point at /jev-hands:setup, which is the only way a first-time user can get
anywhere.

stdout belongs to the JSON-RPC channel. Everything diagnostic goes to stderr.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # never drop __pycache__ into the plugin directory

import atexit  # noqa: E402
import functools  # noqa: E402
import logging  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Dict, List, Optional  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402

from .. import __version__, credentials, doctor as doctor_mod  # noqa: E402
from ..adapters import android_u2  # noqa: E402
from ..config import load_policy  # noqa: E402
from ..core.jev_client import JevClient  # noqa: E402
from ..core.loop import Engine  # noqa: E402
from ..core.models import (  # noqa: E402
    ACTION_BACK,
    ACTION_SCROLL_DOWN,
    ACTION_SCROLL_UP,
    ACTION_TAP,
    ACTION_TYPE,
    OBSERVE_FAILED_HINTS,
    OBSERVE_FAILED_SUMMARIES,
    STOP_REACHED,
    observe_failed_result,
    stop_hint,
)
from ..errors import JevHandsError, UnsupportedPlatformError, error_result  # noqa: E402

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="jev-hands %(levelname)s %(message)s")
LOG = logging.getLogger("jev_hands")

mcp = MCPServer("jev-hands-android", version=__version__)

_engine: Optional[Engine] = None
_serial: Optional[str] = None
_screenshots: Optional[Path] = None


# --- plumbing ----------------------------------------------------------------


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise UnsupportedPlatformError(
            f"jev-hands phase 1 runs on macOS only; this machine reports {sys.platform}."
        )


def _client() -> JevClient:
    key = credentials.get()
    if not key:
        raise JevHandsError(
            "No TypeSafe API key is configured.",
            code="api_key_missing",
            hint="Run /jev-hands:setup to store it, then reconnect this server in /mcp.",
        )
    return JevClient(key)


def _client_or_none() -> Optional[JevClient]:
    key = credentials.get()
    return JevClient(key) if key else None


def _screenshot_dir() -> Path:
    """A private directory under $TMPDIR, thrown away when the server exits.

    Screenshots are scratch: Claude looks at one while the step it belongs to
    is still in hand, and never again. They have no business in the data
    directory, which is for the few things meant to outlive the session.
    """
    global _screenshots
    if _screenshots is None:
        _screenshots = Path(tempfile.mkdtemp(prefix="jev-hands-"))
        atexit.register(shutil.rmtree, _screenshots, ignore_errors=True)
    return _screenshots


def _engine_for(policy_override: Optional[Dict[str, Any]] = None, *, need_key: bool = True) -> Engine:
    """Return the live engine, connecting to the phone the first time."""
    global _engine, _serial
    _require_macos()
    policy = load_policy(policy_override)

    if _engine is None:
        serial = android_u2.select_serial(_serial)
        adapter = android_u2.AndroidAdapter(serial)
        adapter.connect()
        _serial = serial
        _engine = Engine(adapter, None, policy, screenshot_dir=_screenshot_dir())
    _engine.policy = policy
    if _engine.client is None:
        # `need_key` only decides whether a missing key is fatal. A tool that
        # can work without one may still have an optional model-backed part -
        # jev_launch's popup check - so attach the client whenever there is one.
        _engine.client = _client() if need_key else _client_or_none()
    return _engine


def _guard(func):
    """Turn every raised error into a structured result. Tools never throw.

    functools.wraps keeps the original signature visible, which is what the MCP
    layer builds each tool's input schema from.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except JevHandsError as exc:
            LOG.warning("%s: %s", func.__name__, exc.code)
            return exc.to_result()
        except Exception as exc:  # noqa: BLE001
            LOG.exception("%s failed", func.__name__)
            return error_result(
                "internal_error",
                f"{func.__name__} failed unexpectedly ({type(exc).__name__}).",
                "Run /jev-hands:doctor and report the output.",
            )

    return wrapper


def _screen_summary(screen) -> str:
    bits = [f"{len(screen.elements)} element(s)"]
    if screen.app.get("package"):
        bits.append(f"in {screen.app['package']}")
    if screen.truncated:
        bits.append(f"truncated to {screen.truncated['shown']} of {screen.truncated['total']}")
    if screen.unreadable:
        # The one-line reading of it; the full hint is on the screen's `note`.
        bits.append(OBSERVE_FAILED_SUMMARIES[screen.unreadable])
    elif screen.note:
        bits.append(screen.note)
    return ", ".join(bits)


def _reading(value: Optional[float]) -> str:
    """One probability for a summary line, or a dash when it was not answered."""
    return "-" if value is None else f"{value:.2f}"


def _reached_of(step) -> Optional[float]:
    """The step's own `reached` reading, which lives on the decision."""
    return step.decision.reached if step.decision else None


def _decision_summary(decision) -> str:
    target = f" -> {decision.target_name!r} (index {decision.target})" if decision.target_name else ""
    flags = f" flags={decision.flags}" if decision.flags else ""
    return (
        f"{decision.action}{target}, confidence {decision.confidence:.2f} "
        f"[{decision.tier}]{flags}"
    )


# --- tools -------------------------------------------------------------------


@mcp.tool()
@_guard
def jev_doctor(check_device: bool = True, serial: Optional[str] = None) -> Dict[str, Any]:
    """Check everything jev-hands needs and say how to fix what is missing.

    Covers the platform, uv, the data directory, whether an API key is stored
    (presence and origin only, never the value), adb, uiautomator2, the attached
    device and the screen state. Pass check_device=false to keep it away from the
    phone entirely.
    """
    return doctor_mod.run(check_device=check_device, serial=serial)


@mcp.tool()
@_guard
def jev_device(action: str = "list", serial: Optional[str] = None) -> Dict[str, Any]:
    """List attached Android devices, or connect to one.

    With one device attached it selects itself. With several, pass a serial.
    """
    global _engine, _serial
    _require_macos()

    if action == "list":
        devices = android_u2.list_devices()
        return {
            "ok": True,
            "summary": f"{len(devices)} device(s) attached.",
            "devices": devices,
            "selected": _serial,
        }

    if action == "connect":
        chosen = android_u2.select_serial(serial)
        adapter = android_u2.AndroidAdapter(chosen)
        info = adapter.connect()
        _serial = chosen
        _engine = Engine(
            adapter, _client_or_none(), load_policy(), screenshot_dir=_screenshot_dir()
        )
        return {
            "ok": True,
            "summary": f"Connected. uiautomator2 pushed {android_u2.PUSHED_ARTIFACT}; no APK installed.",
            "device": info,
        }

    return error_result(
        "bad_argument",
        f"Unknown action {action!r}.",
        "Use action='list' or action='connect'.",
    )


@mcp.tool()
@_guard
def jev_launch(
    package: str,
    activity: Optional[str] = None,
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Start an app, observe the screen it lands on, and check for a popup.

    Some launchers show a chooser first (work profile, app clone) and some apps
    open onto a full-screen advert or a permission request. Nothing is ever
    dismissed automatically: read `popup` and the candidate table, then decide.
    """
    engine = _engine_for(policy, need_key=False)
    launched = engine.adapter.launch(package, activity)
    # No earlier screen to compare against here, so the whole settle budget
    # is paid up front rather than in two parts.
    time.sleep(engine.policy.settle_total_seconds)
    screen = engine.observe()

    popup: Optional[float] = None
    popup_error: Optional[Dict[str, Any]] = None
    if screen.elements and credentials.get():
        try:
            check = engine.check(
                question=(
                    "Does this screen show a dialog, popup, advertisement, chooser or "
                    "permission request that has to be dealt with before the app itself "
                    "can be used?"
                ),
                screen=screen,
            )
            popup = check["probability"]
        except JevHandsError as exc:
            popup_error = exc.to_result()

    summary = f"Launched {package}. " + _screen_summary(screen)
    result: Dict[str, Any] = {
        "ok": True,
        "summary": summary,
        "launch": launched,
        "screen": screen.to_json(),
        "popup": popup,
    }
    if popup is not None and popup >= engine.policy.thresholds.popup:
        result["hint"] = (
            f"A blocking popup is likely (p={popup:.2f}). Nothing was dismissed. "
            "Pick the right entry from the candidate table with jev_act, or ask the user."
        )
        result["summary"] = summary + f" A blocking popup is likely (p={popup:.2f})."
    if screen.unreadable:
        # The app started and the screen cannot be read. Say which kind of
        # unreadable, because a secure window is not something to retry.
        result["unreadable"] = screen.unreadable
        result["hint"] = OBSERVE_FAILED_HINTS[screen.unreadable]
    if popup_error:
        result["popup_check_error"] = popup_error
    return result


@mcp.tool()
@_guard
def jev_observe(
    screenshot: bool = False,
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Read the current screen into a candidate table.

    No screenshot by default: the loop does not need one and it costs a second.
    Ask for one when the tree comes back empty and a human or Claude has to look.
    """
    engine = _engine_for(policy, need_key=False)
    screen = engine.observe(screenshot=screenshot)
    if screen.unreadable:
        # The read worked and the screen still cannot be used. Saying so with
        # the reason beats handing back an empty candidate table that looks
        # like a page with nothing on it.
        result = observe_failed_result(screen.unreadable)
        result["summary"] = (
            OBSERVE_FAILED_SUMMARIES[screen.unreadable]
            + f" Foreground package: {screen.app.get('package') or 'unknown'}."
        )
        result["screen"] = screen.to_json()
        return result
    return {
        "ok": True,
        "summary": _screen_summary(screen),
        "screen": screen.to_json(),
    }


@mcp.tool()
@_guard
def jev_decide(
    goal: str,
    context: Optional[str] = None,
    text_to_type: Optional[str] = None,
    end_state: Optional[str] = None,
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Ask Jev what to do on the current screen, without doing it.

    Supply `end_state` when the caller has a way to recognise the destination;
    the answer then carries three readings of it, all answered in the same
    request: `reached` as a gate, plus two literal questions -
    `reached_content` (is the content on this screen), which is the second way
    the gate opens, and `reached_entry` (is this screen only a way to it),
    which is what `jev_step` and `jev_run` read the stop off together with
    `reached`. Supply `text_to_type` when the step is meant to fill a field.
    """
    engine = _engine_for(policy)
    screen = engine.observe()
    decision = engine.decide(
        screen,
        goal=goal,
        context=context,
        text_to_type=text_to_type,
        end_state=end_state,
    )
    return {
        "ok": True,
        "summary": _decision_summary(decision),
        "screen": screen.to_json(),
        "decision": decision.to_json(),
    }


@mcp.tool()
@_guard
def jev_step(
    goal: str,
    context: Optional[str] = None,
    text_to_type: Optional[str] = None,
    end_state: Optional[str] = None,
    screenshot_on_stop: Optional[bool] = None,
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run one observe / decide / validate / execute / observe cycle.

    A picture is taken automatically when the step stops on a blocking popup or
    an unreadable tree. Pass screenshot_on_stop=true to also get one on a
    low-confidence stop, or false for never.

    `end_state` is judged three times, in one request. The gate opens when
    `reached` clears `reached_threshold` or `reached_content` clears
    `reached_content_threshold`; the step then stops as `reached` when
    `reached` is over its bar and `reached_entry` at or under
    `reached_entry_threshold`. `reached_content` opens the gate and is
    reported, and nothing else: it overlaps too much between destinations and
    the screens that lead to them to decide a stop. When the two do not agree
    the step does whatever the model picked; when they do not agree and the
    model picked nothing, the step still stops as `reached`, with
    `reached_confirmed: false` and a hint saying to verify.
    `reached_candidate`, `reached_content`, `reached_entry` and
    `reached_confirmed` say which of those happened.
    """
    engine = _engine_for(policy)
    step = engine.step(
        goal=goal,
        context=context,
        text_to_type=text_to_type,
        end_state=end_state,
        screenshot_on_stop=screenshot_on_stop,
    )
    payload = step.to_json()
    if step.stop_reason:
        summary = (
            f"Stopped: {step.stop_reason}. "
            + (
                stop_hint(
                    step.stop_reason,
                    step.reached_confirmed,
                    contradiction_at_end_state=step.contradiction_at_end_state,
                    readings=step.reached_readings(),
                )
                or ""
            )
        )
    elif step.decision:
        summary = "Executed " + _decision_summary(step.decision) + (
            "; the screen changed." if step.changed else "; the screen did not change."
        )
    else:
        summary = "No step was taken."
    if (
        step.reached_candidate
        and step.reached_confirmed is False
        and step.stop_reason != STOP_REACHED
    ):
        summary += (
            f" The `end_state` gate opened but the confirmation did not back "
            f"it up (reached {_reading(_reached_of(step))}, entry "
            f"{_reading(step.reached_entry)}; content "
            f"{_reading(step.reached_content)}), so the step went ahead."
        )
    elif step.stop_reason == STOP_REACHED and step.reached_confirmed is False:
        summary += (
            f" The `end_state` gate opened, the readings came back as reached "
            f"{_reading(_reached_of(step))}, entry {_reading(step.reached_entry)} "
            f"and content {_reading(step.reached_content)}, and the model had no "
            f"action left, so this is probably the destination rather than a "
            f"confirmed one."
        )
    payload["ok"] = step.stop_reason is None and step.error is None
    payload["summary"] = summary
    return payload


@mcp.tool()
@_guard
def jev_run(
    goal: str,
    context: Optional[str] = None,
    text_to_type: Optional[str] = None,
    end_state: Optional[str] = None,
    max_steps: int = 5,
    screenshot_on_stop: Optional[bool] = None,
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Repeat jev_step until something stops it, at most max_steps times.

    Five steps is deliberately small: come back into the loop often rather than
    handing over one goal and sixty steps.

    The result carries `stale_count`, `reused_count`, `gate_hits`, `confirmed`
    and `unconfirmed_stops` next to the steps, so a caller measuring the loop
    does not have to walk them.
    """
    engine = _engine_for(policy)
    result = engine.run(
        goal=goal,
        context=context,
        text_to_type=text_to_type,
        end_state=end_state,
        max_steps=max_steps,
        screenshot_on_stop=screenshot_on_stop,
    )
    payload = result.to_json()
    payload["ok"] = result.stop_reason in (None, "reached")
    last = result.steps[-1] if result.steps else None
    payload["summary"] = (
        f"{len(result.steps)} step(s), stopped on {result.stop_reason}. "
        + (
            stop_hint(
                result.stop_reason,
                result.final_reached_confirmed,
                contradiction_at_end_state=bool(last and last.contradiction_at_end_state),
                readings=last.reached_readings() if last else None,
            )
            or ""
        )
    )
    return payload


@mcp.tool()
@_guard
def jev_act(
    action: str,
    capture_id: str,
    index: Optional[int] = None,
    x: Optional[int] = None,
    y: Optional[int] = None,
    text: Optional[str] = None,
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Carry out an action the caller chose, with no model in the loop.

    `capture_id` must be the observation the index came from; a screen that has
    moved on since then is refused. Actions with no target - go_back, HOME, a
    scroll - are not refused that way: they do not reuse a position from that
    observation, and a page that never settles has to stay escapable.
    """
    allowed = (ACTION_TAP, ACTION_TYPE, ACTION_SCROLL_DOWN, ACTION_SCROLL_UP, ACTION_BACK, "press")
    if action not in allowed:
        return error_result(
            "bad_argument",
            f"Unknown action {action!r}.",
            f"Use one of: {', '.join(allowed)}.",
        )
    engine = _engine_for(policy, need_key=False)
    step = engine.act(
        action=action, capture_id=capture_id, index=index, x=x, y=y, text=text
    )
    payload = step.to_json()
    payload["ok"] = step.stop_reason is None and step.error is None
    payload["summary"] = (
        f"{action} done; the screen "
        + ("changed." if step.changed else "did not change.")
        if step.executed
        else f"{action} was not carried out: {step.stop_reason}."
    )
    return payload


@mcp.tool()
@_guard
def jev_check(
    question: str,
    criteria: Optional[Dict[str, str]] = None,
    policy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Ask one yes/no question about the current screen and get a probability.

    Useful for destination checks, popup classification, or any other yes/no
    judgement the caller wants to make on its own terms. `criteria` takes
    optional `true` and `false` descriptions.

    `probability` is the raw answer. `tier` reads it for you as `yes`, `no` or
    `unsure`, cut at `check_threshold` and its mirror image.
    """
    engine = _engine_for(policy)
    result = engine.check(question=question, criteria=criteria)
    result["ok"] = True
    result["summary"] = (
        f"Probability {result['probability']:.2f} ({result['tier']}) for: {question}"
    )
    return result


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
