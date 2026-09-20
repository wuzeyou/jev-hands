"""The environment report. The device probe is switched off throughout."""

from __future__ import annotations

import pytest

from jev_hands import credentials, doctor


@pytest.fixture(autouse=True)
def no_device(monkeypatch, tmp_path):
    monkeypatch.setenv(doctor.SKIP_DEVICE_ENV, "1")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    credentials.reset_cache()
    yield
    credentials.reset_cache()


def test_every_row_has_the_same_shape():
    report = doctor.run()
    assert set(report) >= {"ok", "summary", "checks", "config"}
    names = [row["check"] for row in report["checks"]]
    for expected in ("platform", "python", "uv", "data_dir", "api_key", "adb", "device"):
        assert expected in names
    for row in report["checks"]:
        assert set(row) <= {"check", "status", "detail", "fix"}
        assert row["status"] in (doctor.OK, doctor.WARN, doctor.FAIL, doctor.SKIPPED)
        if row["status"] == doctor.FAIL:
            assert row.get("fix")


def test_the_device_probe_can_be_switched_off(monkeypatch):
    calls = []
    monkeypatch.setattr(doctor.android_u2, "list_devices", lambda *a, **k: calls.append(1) or [])
    report = doctor.run(check_device=False)
    device_row = next(r for r in report["checks"] if r["check"] == "device")
    assert device_row["status"] == doctor.SKIPPED
    assert calls == []


def test_a_missing_key_points_at_the_setup_command(monkeypatch):
    monkeypatch.setattr(credentials, "exists", lambda: (False, credentials.SOURCE_NONE))
    report = doctor.run(check_device=False)
    row = next(r for r in report["checks"] if r["check"] == "api_key")
    assert row["status"] == doctor.FAIL
    assert row["fix"] == "/jev-hands:setup"
    assert report["ok"] is False


def test_the_key_origin_is_reported_but_never_the_key(monkeypatch):
    monkeypatch.setattr(credentials, "exists", lambda: (True, credentials.SOURCE_ENV))
    report = doctor.run(check_device=False)
    row = next(r for r in report["checks"] if r["check"] == "api_key")
    assert row["status"] == doctor.OK
    assert "environment variable" in row["detail"]

    monkeypatch.setattr(credentials, "exists", lambda: (True, credentials.SOURCE_KEYCHAIN))
    row = next(r for r in doctor.run(check_device=False)["checks"] if r["check"] == "api_key")
    assert "keychain" in row["detail"]


def test_the_fallback_data_directory_is_called_out(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.setattr(doctor, "data_dir", lambda: (tmp_path / "fallback", True))
    row = next(r for r in doctor.run(check_device=False)["checks"] if r["check"] == "data_dir")
    assert row["status"] == doctor.WARN
    assert "fallback" in row["detail"]


def test_the_report_says_what_was_pushed_to_the_phone_and_how_to_remove_it():
    row = next(
        r for r in doctor.run(check_device=False)["checks"] if r["check"] == "phone_footprint"
    )
    assert "/data/local/tmp/u2.jar" in row["detail"]
    assert "no APK" in row["detail"]
    assert "rm /data/local/tmp/u2.jar" in row["fix"]


def test_the_text_rendering_keeps_every_row_and_fix():
    report = doctor.run(check_device=False)
    text = doctor.as_text(report)
    for row in report["checks"]:
        assert row["check"] in text
        if row.get("fix"):
            assert row["fix"] in text


def test_version_comparison():
    assert doctor._version_at_least("3.7.0", (3, 7))
    assert doctor._version_at_least("3.10.2", (3, 7))
    assert not doctor._version_at_least("3.6.9", (3, 7))
    assert not doctor._version_at_least("2.16.0", (3, 7))
