"""Reading the API key. The `security` subprocess is mocked throughout."""

from __future__ import annotations

import subprocess

import pytest

from jev_hands import credentials


class FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    credentials.reset_cache()
    monkeypatch.delenv(credentials.ENV_VAR, raising=False)
    yield
    credentials.reset_cache()


def patch_security(monkeypatch, handler):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((list(args), kwargs))
        return handler(list(args))

    monkeypatch.setattr(credentials.subprocess, "run", fake_run)
    return calls


def test_the_environment_variable_wins_and_is_reported_as_such(monkeypatch):
    monkeypatch.setenv(credentials.ENV_VAR, "  key-from-env  ")
    calls = patch_security(monkeypatch, lambda args: FakeProc(0, "key-from-keychain\n"))
    assert credentials.get() == "key-from-env"
    assert credentials.exists() == (True, credentials.SOURCE_ENV)
    assert calls == []


def test_an_empty_environment_variable_does_not_count(monkeypatch):
    monkeypatch.setenv(credentials.ENV_VAR, "   ")
    monkeypatch.setattr(credentials, "is_macos", lambda: True)
    patch_security(monkeypatch, lambda args: FakeProc(credentials.EXIT_ITEM_NOT_FOUND))
    assert credentials.exists() == (False, credentials.SOURCE_NONE)


def test_the_existence_check_never_asks_for_the_value(monkeypatch):
    monkeypatch.setattr(credentials, "is_macos", lambda: True)
    calls = patch_security(monkeypatch, lambda args: FakeProc(0))
    found, origin = credentials.exists()
    assert (found, origin) == (True, credentials.SOURCE_KEYCHAIN)
    assert len(calls) == 1
    assert "-w" not in calls[0][0]


def test_exit_code_44_means_no_key_is_stored(monkeypatch):
    monkeypatch.setattr(credentials, "is_macos", lambda: True)
    patch_security(monkeypatch, lambda args: FakeProc(credentials.EXIT_ITEM_NOT_FOUND))
    assert credentials.exists() == (False, credentials.SOURCE_NONE)
    assert credentials.get() is None


def test_any_other_non_zero_exit_is_a_real_failure_not_an_absence(monkeypatch, caplog):
    """A locked keychain returns something else. Reporting that as "no key"
    would send the user off to store a key they already have."""
    monkeypatch.setattr(credentials, "is_macos", lambda: True)
    patch_security(monkeypatch, lambda args: FakeProc(51, stderr="keychain is locked"))
    with caplog.at_level("WARNING"):
        found, origin = credentials.exists()
    assert (found, origin) == (False, credentials.SOURCE_NONE)
    assert any("51" in record.getMessage() for record in caplog.records)


def test_a_stored_key_is_read_and_cached(monkeypatch):
    monkeypatch.setattr(credentials, "is_macos", lambda: True)
    calls = patch_security(monkeypatch, lambda args: FakeProc(0, "stored-key\n"))
    assert credentials.get() == "stored-key"
    assert credentials.get() == "stored-key"
    assert len(calls) == 1
    assert credentials.cached_source() == credentials.SOURCE_KEYCHAIN


def test_the_key_is_never_placed_on_a_command_line(monkeypatch):
    monkeypatch.setattr(credentials, "is_macos", lambda: True)
    calls = patch_security(monkeypatch, lambda args: FakeProc(0, "stored-key\n"))
    credentials.get()
    for args, kwargs in calls:
        assert "stored-key" not in " ".join(args)
        assert kwargs.get("stdin") is subprocess.DEVNULL


def test_a_timeout_is_survived_rather_than_raised(monkeypatch):
    monkeypatch.setattr(credentials, "is_macos", lambda: True)

    def boom(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="security", timeout=10)

    monkeypatch.setattr(credentials.subprocess, "run", boom)
    assert credentials.get() is None
    assert credentials.exists() == (False, credentials.SOURCE_NONE)


def test_off_macos_the_answer_is_unsupported_platform(monkeypatch):
    monkeypatch.setattr(credentials, "is_macos", lambda: False)
    calls = patch_security(monkeypatch, lambda args: FakeProc(0, "should not happen"))
    found, origin = credentials.exists()
    assert (found, origin) == (False, credentials.SOURCE_UNSUPPORTED)
    assert credentials.get() is None
    assert calls == []


def test_the_module_exposes_no_way_to_print_the_value():
    public = {name for name in dir(credentials) if not name.startswith("_")}
    assert "get" in public and "exists" in public and "source" in public
    assert not {name for name in public if "print" in name or "show" in name}


def test_a_ten_second_ceiling_is_always_applied(monkeypatch):
    monkeypatch.setattr(credentials, "is_macos", lambda: True)
    calls = patch_security(monkeypatch, lambda args: FakeProc(0))
    credentials.exists()
    assert calls[0][1]["timeout"] == credentials.SECURITY_TIMEOUT == 10.0
