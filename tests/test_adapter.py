"""adb discovery, device selection and the execute mapping. No phone involved."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import labels as L
from jev_hands.adapters import android_u2 as A
from jev_hands.errors import DeviceError


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


DEVICES_ONE = "List of devices attached\nSERIAL1     device product:x model:y\n"
DEVICES_TWO = "List of devices attached\nSERIAL1  device\nSERIAL2  device\n"
DEVICES_UNAUTH = "List of devices attached\nSERIAL1  unauthorized\n"


def patch_adb(monkeypatch, output, adb="/fake/adb"):
    monkeypatch.setattr(A, "find_adb", lambda: adb)
    monkeypatch.setattr(A, "_run_adb", lambda binary, args, timeout=15.0: FakeProc(0, output))


def test_adb_is_looked_for_on_path_first(monkeypatch):
    monkeypatch.setattr(A.shutil, "which", lambda name: "/usr/bin/adb")
    assert A.find_adb() == "/usr/bin/adb"


def test_adb_falls_back_to_android_home_then_sdk_root(tmp_path, monkeypatch):
    monkeypatch.setattr(A.shutil, "which", lambda name: None)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setattr(A, "COMMON_ADB_DIRS", ())
    assert A.find_adb() is None

    sdk = tmp_path / "sdk" / "platform-tools"
    sdk.mkdir(parents=True)
    binary = sdk / "adb"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)

    monkeypatch.setenv("ANDROID_HOME", str(tmp_path / "sdk"))
    assert A.find_adb() == str(binary)

    monkeypatch.delenv("ANDROID_HOME")
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(tmp_path / "sdk"))
    assert A.find_adb() == str(binary)


def test_a_missing_adb_is_a_structured_error_with_an_install_hint(monkeypatch):
    monkeypatch.setattr(A, "find_adb", lambda: None)
    with pytest.raises(DeviceError) as caught:
        A.list_devices()
    assert caught.value.code == "adb_missing"
    assert "platform tools" in caught.value.hint
    assert caught.value.to_result()["ok"] is False


def test_one_device_selects_itself(monkeypatch):
    patch_adb(monkeypatch, DEVICES_ONE)
    assert A.select_serial() == "SERIAL1"


def test_several_devices_need_an_explicit_serial(monkeypatch):
    patch_adb(monkeypatch, DEVICES_TWO)
    with pytest.raises(DeviceError) as caught:
        A.select_serial()
    assert caught.value.code == "ambiguous_device"
    assert len(caught.value.details["devices"]) == 2
    assert A.select_serial("SERIAL2") == "SERIAL2"


def test_an_unauthorized_device_says_what_to_do_on_the_phone(monkeypatch):
    patch_adb(monkeypatch, DEVICES_UNAUTH)
    with pytest.raises(DeviceError) as caught:
        A.select_serial()
    assert caught.value.code == "device_unauthorized"
    assert "Allow" in caught.value.hint


def test_no_device_at_all(monkeypatch):
    patch_adb(monkeypatch, "List of devices attached\n")
    with pytest.raises(DeviceError) as caught:
        A.select_serial()
    assert caught.value.code == "no_device"


def test_an_unknown_serial_is_rejected(monkeypatch):
    patch_adb(monkeypatch, DEVICES_ONE)
    with pytest.raises(DeviceError) as caught:
        A.select_serial("NOPE")
    assert caught.value.code == "device_not_found"


def test_adb_never_runs_with_an_open_stdin(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        return FakeProc(0, DEVICES_ONE)

    monkeypatch.setattr(A.subprocess, "run", fake_run)
    A._run_adb("/fake/adb", ["devices", "-l"])
    assert seen["stdin"] is subprocess.DEVNULL


class StubDevice:
    def __init__(self):
        self.clicks = []
        self.texts = []
        self.swipes = []
        self.presses = []

    def click(self, x, y):
        self.clicks.append((x, y))

    def window_size(self):
        return (1000, 2000)

    def swipe(self, *args):
        self.swipes.append(args)

    def press(self, key):
        self.presses.append(key)

    def __call__(self, **kwargs):
        assert kwargs == {"focused": True}
        return self

    def set_text(self, text):
        self.texts.append(text)


def adapter_with_stub():
    adapter = A.AndroidAdapter("SERIAL1", adb="/fake/adb")
    stub = StubDevice()
    adapter._device = stub
    return adapter, stub


def test_tap_uses_the_coordinates_the_code_computed():
    adapter, stub = adapter_with_stub()
    adapter.execute({"type": "tap_element", "x": 12, "y": 34})
    assert stub.clicks == [(12, 34)]


def test_typing_focuses_then_uses_the_accessibility_set_text_path():
    adapter, stub = adapter_with_stub()
    adapter.execute({"type": "type_text", "x": 5, "y": 6, "text": L.MILK_TEA})
    assert stub.clicks == [(5, 6)]
    assert stub.texts == [L.MILK_TEA]


def test_scrolling_and_back_map_onto_swipe_and_press():
    adapter, stub = adapter_with_stub()
    adapter.execute({"type": "scroll_down"})
    adapter.execute({"type": "scroll_up"})
    adapter.execute({"type": "go_back"})
    assert len(stub.swipes) == 2
    assert stub.swipes[0][1] > stub.swipes[0][3]  # down drags upwards
    assert stub.swipes[1][1] < stub.swipes[1][3]
    assert stub.presses == ["back"]


def test_an_unknown_action_is_refused_not_guessed():
    from jev_hands.errors import JevHandsError

    adapter, _ = adapter_with_stub()
    with pytest.raises(JevHandsError) as caught:
        adapter.execute({"type": "teleport"})
    assert caught.value.code == "bad_action"


def test_a_failing_action_reports_an_unknown_outcome_rather_than_retrying():
    from jev_hands.errors import JevHandsError

    adapter = A.AndroidAdapter("SERIAL1", adb="/fake/adb")

    class Broken(StubDevice):
        def click(self, x, y):
            raise RuntimeError("the socket went away")

    adapter._device = Broken()
    with pytest.raises(JevHandsError) as caught:
        adapter.execute({"type": "tap_element", "x": 1, "y": 2})
    assert caught.value.code == "execute_uncertain"
    assert "Never repeat a write blindly" in caught.value.hint


class LaunchStub(StubDevice):
    def __init__(self):
        super().__init__()
        self.started = []

    def app_start(self, package):
        self.started.append(package)


RESOLVE_OUTPUT = (
    "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=false\n"
    "com.example.app/.MainActivity\n"
)


def test_launch_without_an_activity_resolves_the_component_itself(monkeypatch):
    """uiautomator2's app_start() falls back to `monkey` when no activity is
    given, and some apps never come up under it. Resolve the launcher component
    through the package manager and start it explicitly instead."""
    adapter = A.AndroidAdapter("SERIAL1", adb="/fake/adb")
    stub = LaunchStub()
    adapter._device = stub
    calls = []

    def fake_run(binary, args, timeout=15.0):
        calls.append(list(args))
        if "resolve-activity" in args:
            return FakeProc(0, stdout=RESOLVE_OUTPUT)
        return FakeProc(0)

    monkeypatch.setattr(A, "_run_adb", fake_run)
    result = adapter.launch("com.example.app")
    assert stub.started == [], "app_start must not be used once the component is known"
    assert result["via"] == "am_start_resolved"
    assert result["activity"] == ".MainActivity"
    assert calls[-1] == [
        "-s", "SERIAL1", "shell", "am", "start", "-n", "com.example.app/.MainActivity"
    ]


def test_launch_falls_back_to_the_library_when_nothing_resolves(monkeypatch):
    """The package manager not resolving a component is the only case left
    where uiautomator2's own route is worth trying."""
    adapter = A.AndroidAdapter("SERIAL1", adb="/fake/adb")
    stub = LaunchStub()
    adapter._device = stub
    monkeypatch.setattr(A, "_run_adb", lambda *a, **k: FakeProc(0, stdout="No activity found\n"))

    result = adapter.launch("com.example.app")
    assert stub.started == ["com.example.app"]
    assert result["via"] == "app_start"


def test_launch_with_an_activity_uses_am_start_with_a_component(monkeypatch):
    adapter = A.AndroidAdapter("SERIAL1", adb="/fake/adb")
    adapter._device = LaunchStub()
    seen = {}

    def fake_run(binary, args, timeout=15.0):
        seen["binary"] = binary
        seen["args"] = list(args)
        return FakeProc(0)

    monkeypatch.setattr(A, "_run_adb", fake_run)
    result = adapter.launch("com.example.app", ".MainActivity")
    assert seen["args"] == [
        "-s", "SERIAL1", "shell", "am", "start", "-n", "com.example.app/.MainActivity"
    ]
    assert result["via"] == "am_start"
    assert adapter._device.started == [], "app_start must not be used when an activity is given"


def test_monkey_is_never_used_as_a_command():
    """Some apps never come up under `monkey`, so it is not a launch route."""
    source = Path(A.__file__).read_text(encoding="utf-8")
    assert '"monkey"' not in source
    assert "'monkey'" not in source


def test_a_failed_launch_becomes_a_structured_error(monkeypatch):
    adapter = A.AndroidAdapter("SERIAL1", adb="/fake/adb")
    monkeypatch.setattr(A, "_run_adb", lambda *a, **k: FakeProc(0, stdout="No activity found\n"))

    class Broken(LaunchStub):
        def app_start(self, package):
            raise RuntimeError("no such package")

    adapter._device = Broken()
    with pytest.raises(DeviceError) as caught:
        adapter.launch("com.nope")
    assert caught.value.code == "launch_failed"
    assert "pm list packages" in caught.value.hint


def test_the_reconnect_settings_match_what_the_phone_needs():
    assert A.CONNECT_ATTEMPTS == 3
    assert A.CONNECT_GAP_SECONDS == 2.0
    assert A.PUSHED_ARTIFACT == "/data/local/tmp/u2.jar"


def test_the_default_input_method_is_read_once_and_cached(monkeypatch):
    """The fingerprint skips input-method windows. Asking the phone which one
    is current makes that exact rather than a guess at the package name, and
    one adb call per adapter is cheap enough to be worth it."""
    adapter = A.AndroidAdapter("SERIAL1", adb="/fake/adb")
    calls = []

    def fake_run(binary, args, timeout=15.0):
        calls.append(list(args))
        return FakeProc(0, stdout="com.vendor.typing/.TypingService\n")

    monkeypatch.setattr(A, "_run_adb", fake_run)
    assert adapter.default_ime_package() == "com.vendor.typing"
    assert adapter.default_ime_package() == "com.vendor.typing"
    assert len(calls) == 1
    assert calls[0] == [
        "-s", "SERIAL1", "shell", "settings", "get", "secure", "default_input_method"
    ]


def test_an_unreadable_default_input_method_is_not_asked_for_again(monkeypatch):
    adapter = A.AndroidAdapter("SERIAL1", adb="/fake/adb")
    calls = []

    def fake_run(binary, args, timeout=15.0):
        calls.append(list(args))
        return FakeProc(0, stdout="null\n")

    monkeypatch.setattr(A, "_run_adb", fake_run)
    assert adapter.default_ime_package() is None
    assert adapter.default_ime_package() is None
    assert len(calls) == 1
