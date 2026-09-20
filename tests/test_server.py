"""The MCP tool surface, checked without starting a phone or a network call."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from jev_hands.servers import android as server

EXPECTED_TOOLS = {
    "jev_doctor",
    "jev_device",
    "jev_launch",
    "jev_observe",
    "jev_decide",
    "jev_step",
    "jev_run",
    "jev_act",
    "jev_check",
}


def tool_names():
    return {tool.name for tool in asyncio.run(server.mcp.list_tools())}


def test_exactly_the_nine_tools_are_registered():
    assert tool_names() == EXPECTED_TOOLS


def test_every_tool_is_documented_for_the_caller():
    for tool in asyncio.run(server.mcp.list_tools()):
        assert tool.description and len(tool.description) > 40, tool.name


def test_tool_arguments_match_the_design():
    # The SDK attribute is `input_schema`; `inputSchema` is only its wire alias.
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    properties = {
        name: set((tool.input_schema or {}).get("properties", {}))
        for name, tool in tools.items()
    }
    assert properties["jev_device"] >= {"action", "serial"}
    assert properties["jev_launch"] >= {"package", "activity"}
    assert properties["jev_observe"] >= {"screenshot"}
    assert properties["jev_decide"] >= {"goal", "context", "text_to_type", "end_state"}
    assert properties["jev_step"] >= {"goal", "policy", "screenshot_on_stop"}
    assert properties["jev_run"] >= {"goal", "max_steps", "screenshot_on_stop"}
    assert properties["jev_act"] >= {"action", "index", "x", "y", "text", "capture_id"}
    assert properties["jev_check"] >= {"question", "criteria"}


def test_a_tool_result_reaches_the_caller_as_the_plain_object(monkeypatch):
    """What Claude reads is the text content. It has to stay the bare result
    object, not a wrapper the SDK put around it."""
    monkeypatch.setenv(server.doctor_mod.SKIP_DEVICE_ENV, "1")
    result = asyncio.run(server.mcp.call_tool("jev_doctor", {"check_device": False}))
    payload = json.loads(result.content[0].text)
    assert set(payload) >= {"ok", "summary", "checks"}
    assert "result" not in payload


def test_a_missing_api_key_is_a_structured_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(server.credentials, "get", lambda: None)
    with pytest.raises(Exception) as caught:
        server._client()
    assert caught.value.code == "api_key_missing"
    result = caught.value.to_result()
    assert result["ok"] is False
    assert "/jev-hands:setup" in result["hint"]


def test_the_guard_turns_any_exception_into_a_result():
    @server._guard
    def explode():
        raise ValueError("boom")

    result = explode()
    assert result["ok"] is False
    assert result["error"] == "internal_error"
    assert "summary" in result


def test_doctor_returns_a_structured_result_without_touching_the_phone(monkeypatch):
    monkeypatch.setenv(server.doctor_mod.SKIP_DEVICE_ENV, "1")
    result = server.jev_doctor(check_device=False)
    assert "summary" in result
    assert isinstance(result["checks"], list)
    json.dumps(result)


def test_off_macos_every_tool_reports_unsupported_platform(monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "linux")
    result = server.jev_observe()
    assert result["error"] == "unsupported_platform"
    assert "macOS" in result["summary"]


def test_jev_act_rejects_an_action_it_does_not_know():
    result = server.jev_act(action="teleport", capture_id="c_0")
    assert result["ok"] is False
    assert result["error"] == "bad_argument"


def test_jev_device_rejects_an_unknown_sub_action(monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "darwin")
    result = server.jev_device(action="explode")
    assert result["error"] == "bad_argument"


def test_launch_checks_for_a_popup_and_never_dismisses_it(monkeypatch):
    """Design rule: the plugin reports a blocking popup, the caller decides."""
    from jev_hands.core.loop import Engine
    from jev_hands.core.models import Screen

    screen = Screen(
        capture_id="c_1",
        captured_at=0.0,
        app={"package": "com.example", "activity": None},
        elements=[],
        fingerprint="sha1:x",
    )
    from jev_hands.core.models import Element

    screen.elements = [
        Element(index=0, name="Close", id=None, type="Button", at=(1, 1), bounds=(0, 0, 2, 2))
    ]

    class StubAdapter:
        name = "stub"

        def launch(self, package, activity=None):
            return {"package": package, "activity": activity, "via": "app_start"}

        def observe(self, **kwargs):
            return screen

    engine = Engine(StubAdapter(), None, server.load_policy())
    monkeypatch.setattr(server, "_engine_for", lambda *a, **k: engine)
    monkeypatch.setattr(server.credentials, "get", lambda: "fake-key")
    monkeypatch.setattr(engine, "check", lambda **kw: {"probability": 0.82, "tier": "act"})
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)

    result = server.jev_launch(package="com.example")
    assert result["popup"] == 0.82
    assert "hint" in result and "Nothing was dismissed" in result["hint"]
    assert result["launch"]["via"] == "app_start"


def test_launch_still_works_without_a_key_and_reports_no_popup(monkeypatch):
    from jev_hands.core.loop import Engine
    from jev_hands.core.models import Element, Screen

    screen = Screen(
        capture_id="c_1",
        captured_at=0.0,
        app={"package": "com.example", "activity": None},
        elements=[Element(index=0, name="Close", id=None, type="Button", at=(1, 1), bounds=(0, 0, 2, 2))],
        fingerprint="sha1:x",
    )

    class StubAdapter:
        name = "stub"

        def launch(self, package, activity=None):
            return {"package": package, "activity": activity, "via": "app_start"}

        def observe(self, **kwargs):
            return screen

    engine = Engine(StubAdapter(), None, server.load_policy())
    monkeypatch.setattr(server, "_engine_for", lambda *a, **k: engine)
    monkeypatch.setattr(server.credentials, "get", lambda: None)
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)

    result = server.jev_launch(package="com.example")
    assert result["ok"] is True
    assert result["popup"] is None


def test_observe_reports_a_secure_screen_instead_of_an_empty_table(monkeypatch):
    """A biometric prompt, a PIN pad or an app hiding its tree all come back as
    a window with nothing in it. Handing that over as a page with no elements
    reads like a page with nothing on it, which is a different problem."""
    from conftest import fixture_text

    from jev_hands.core import candidates as candidates_mod
    from jev_hands.core.loop import Engine
    from jev_hands.core.models import OBSERVE_HIDDEN_TREE

    screen = candidates_mod.build_screen(fixture_text("chat_empty_tree.xml"))

    class StubAdapter:
        name = "stub"

        def observe(self, **kwargs):
            return screen

    engine = Engine(StubAdapter(), None, server.load_policy())
    monkeypatch.setattr(server, "_engine_for", lambda *a, **k: engine)

    result = server.jev_observe()
    assert result["ok"] is False
    assert result["error"] == "observe_failed"
    assert result["reason"] == OBSERVE_HIDDEN_TREE
    assert "secure window" in result["hint"]
    assert result["screen"]["unreadable"] == OBSERVE_HIDDEN_TREE
    assert result["screen"]["app"]["package"] == "com.example.chat"


def test_the_package_never_mentions_the_forbidden_input_helpers():
    """The clipboard helper triggers an APK install that vendor security blocks;
    the input-method switch changes the user's keyboard. Neither may appear."""
    root = Path(server.__file__).resolve().parents[1]
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "send_" + "keys" not in text, path
        assert "set_input_" + "ime" not in text, path


def test_the_package_never_grows_a_word_list_back():
    """The word lists were removed on purpose: nothing in the plugin decides
    from an element's name. Spelled in pieces so this guard does not itself
    trip the grep it stands for."""
    root = Path(server.__file__).resolve().parents[1]
    forbidden = ("deny" + "list", "risk" + "_word", "word" + "list")
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for word in forbidden:
            assert word not in text, f"{path}: {word}"


def test_a_key_only_tool_still_gets_a_client_on_the_first_call(monkeypatch):
    """jev_launch asks for an engine with need_key=False, then runs an optional
    model-backed popup check on it. The client has to be there for that, or the
    very first launch of a session silently reports no popup."""
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server, "_engine", None)
    monkeypatch.setattr(server, "_serial", None)
    monkeypatch.setattr(server.credentials, "get", lambda: "fake-key")
    monkeypatch.setattr(server.android_u2, "select_serial", lambda serial: "SERIAL1")

    class StubAdapter:
        def __init__(self, serial):
            self.serial = serial

        def connect(self):
            return {"serial": self.serial}

    monkeypatch.setattr(server.android_u2, "AndroidAdapter", StubAdapter)

    engine = server._engine_for(None, need_key=False)
    assert engine.client is not None, "the popup check would raise api_key_missing"


def test_jev_check_hands_back_the_probability_and_its_reading(monkeypatch):
    """The raw probability is what a caller may want to threshold itself; the
    tier is the plugin's own reading of it against `check_threshold`."""
    from jev_hands.core.loop import Engine

    class StubAdapter:
        name = "stub"

    engine = Engine(StubAdapter(), None, server.load_policy())
    monkeypatch.setattr(server, "_engine_for", lambda *a, **k: engine)
    monkeypatch.setattr(
        engine,
        "check",
        lambda **kw: {"probability": 0.93, "tier": "yes", "capture_id": "c_1"},
    )

    result = server.jev_check(question="is the orders list showing?")
    assert result["ok"] is True
    assert result["probability"] == 0.93
    assert result["tier"] == "yes"
    assert "yes" in result["summary"]
def _engine_at_a_probable_destination():
    """An engine whose one canned answer opens the `reached` gate, fails both
    confirmations and offers no action - the round 6 shape that ended two runs
    on `stop_none` while standing on the destination."""
    from conftest import FakeAdapter, FakeClient, Turn, fixture_text

    from jev_hands.core.loop import Engine
    from jev_hands.core.policy import Policy

    return Engine(
        FakeAdapter([fixture_text("delivery_home.xml")]),
        FakeClient(
            [
                Turn(
                    action="none",
                    action_confidence=0.8,
                    tap="none",
                    reached=0.9,
                    content=0.2,
                    entry=0.8,
                )
            ]
        ),
        Policy(settle_seconds=0.0),
    )


def test_jev_step_says_a_stop_the_confirmation_did_not_back_up(monkeypatch):
    monkeypatch.setattr(server, "_engine_for", lambda *a, **k: _engine_at_a_probable_destination())
    result = server.jev_step(
        goal="open the orders list", end_state="the orders list is on screen"
    )
    assert result["stop_reason"] == "reached"
    assert result["reached_confirmed"] is False
    assert "jev_check" in result["stop_hint"]
    assert "probably the destination" in result["summary"]


def test_jev_run_counts_the_gate_hits_and_the_unconfirmed_stops(monkeypatch):
    monkeypatch.setattr(server, "_engine_for", lambda *a, **k: _engine_at_a_probable_destination())
    result = server.jev_run(
        goal="open the orders list",
        end_state="the orders list is on screen",
        max_steps=2,
    )
    assert result["stop_reason"] == "reached"
    assert result["gate_hits"] == 1
    assert result["confirmed"] == 0
    assert result["unconfirmed_stops"] == 1
    assert result["ok"] is True
    assert "jev_check" in result["summary"]
