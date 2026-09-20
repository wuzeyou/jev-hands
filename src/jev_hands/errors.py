"""Structured errors.

Every failure that reaches an MCP tool is turned into a plain JSON object with a
stable ``code``, a human readable ``summary`` and a ``hint`` telling the caller
what to do next. Tools never raise; they return.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class JevHandsError(Exception):
    """Base class for everything this package raises internally."""

    code = "internal_error"
    hint = "Run /jev-hands:doctor and report the output."

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        hint: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if hint:
            self.hint = hint
        self.details = details or {}

    def to_result(self) -> Dict[str, Any]:
        return error_result(self.code, self.message, self.hint, self.details)


class UnsupportedPlatformError(JevHandsError):
    code = "unsupported_platform"
    hint = "jev-hands phase 1 supports macOS only."


class CredentialsMissingError(JevHandsError):
    code = "api_key_missing"
    hint = "Run /jev-hands:setup to store the TypeSafe API key."


class DeviceError(JevHandsError):
    code = "device_error"
    hint = "Run /jev-hands:doctor to check the adb and device link."


class ObserveError(JevHandsError):
    code = "observe_failed"
    hint = "Take a screenshot with jev_observe(screenshot=true) and look at it instead."


class JevApiError(JevHandsError):
    code = "jev_api_error"
    hint = "Check network access to api.typesafe.ai and the stored API key."


def error_result(
    code: str,
    summary: str,
    hint: str = "",
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the JSON object every failing tool returns."""
    out: Dict[str, Any] = {"ok": False, "error": code, "summary": summary}
    if hint:
        out["hint"] = hint
    if details:
        out["details"] = details
    return out
