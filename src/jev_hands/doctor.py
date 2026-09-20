"""Environment check with a fix for every problem it finds.

Each row is the same shape: ``check`` / ``status`` / ``detail`` / ``fix``.
Nothing here ever prints a credential; the key row says configured or not and
where it comes from.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any, Dict, List, Optional

from . import credentials
from .adapters import android_u2
from .config import config_sources, data_dir

OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIPPED = "skipped"

# Set this to 1 to keep the doctor away from the phone entirely. Useful in CI
# and when checking the server without a device attached.
SKIP_DEVICE_ENV = "JEV_HANDS_SKIP_DEVICE_PROBE"

UV_INSTALL_HINT = (
    "Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh`, then start a new "
    "Claude Code session so the MCP server can find it."
)


def _row(
    check: str,
    status: str,
    detail: str,
    fix: Optional[str] = None,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {"check": check, "status": status, "detail": detail}
    if fix:
        row["fix"] = fix
    return row


def skip_device_probe(explicit: Optional[bool] = None) -> bool:
    if explicit is not None:
        return not explicit
    return os.environ.get(SKIP_DEVICE_ENV, "") not in ("", "0", "false", "no")


def run(
    *,
    check_device: Optional[bool] = None,
    serial: Optional[str] = None,
) -> Dict[str, Any]:
    """Run every check and return the report plus a one line summary."""
    rows: List[Dict[str, Any]] = []

    # 1. platform
    if sys.platform == "darwin":
        rows.append(_row("platform", OK, f"macOS ({sys.platform})"))
    else:
        rows.append(
            _row(
                "platform",
                FAIL,
                f"{sys.platform} is not supported in phase 1",
                "jev-hands supports macOS only for now.",
            )
        )

    # 2. python
    version = ".".join(str(p) for p in sys.version_info[:3])
    rows.append(
        _row("python", OK if sys.version_info >= (3, 10) else FAIL, version,
             None if sys.version_info >= (3, 10) else "Python 3.10 or newer is required.")
    )

    # 3. uv
    uv_path = shutil.which("uv")
    rows.append(
        _row("uv", OK if uv_path else WARN, uv_path or "not on PATH",
             None if uv_path else UV_INSTALL_HINT)
    )

    # 4. data directory
    directory, is_fallback = data_dir()
    writable = True
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError:
        writable = False
    if not writable:
        rows.append(
            _row("data_dir", FAIL, f"{directory} is not writable",
                 "Fix the directory permissions or unset CLAUDE_PLUGIN_DATA.")
        )
    elif is_fallback:
        rows.append(
            _row(
                "data_dir",
                WARN,
                f"{directory} (fallback: CLAUDE_PLUGIN_DATA is not set)",
                "Normal when the server is started by hand; Claude Code sets it for plugins.",
            )
        )
    else:
        rows.append(_row("data_dir", OK, str(directory)))

    # 5. API key: presence and origin only, never the value
    configured, origin = credentials.exists()
    if configured and origin == credentials.SOURCE_ENV:
        rows.append(
            _row("api_key", OK, "configured, from the TYPESAFE_API_KEY environment variable",
                 "This overrides the keychain. Unset it to use the stored key instead.")
        )
    elif configured:
        rows.append(
            _row("api_key", OK,
                 f"configured, from the macOS keychain (service {credentials.SERVICE})")
        )
    elif origin == credentials.SOURCE_UNSUPPORTED:
        rows.append(
            _row("api_key", FAIL, "no keychain on this platform", "jev-hands supports macOS only.")
        )
    else:
        rows.append(_row("api_key", FAIL, "not configured", "/jev-hands:setup"))

    # 6. uiautomator2
    try:
        import uiautomator2  # noqa: WPS433,F401

        u2_version = _package_version("uiautomator2") or getattr(
            uiautomator2, "__version__", "unknown"
        )
        good = _version_at_least(u2_version, (3, 7))
        rows.append(
            _row(
                "uiautomator2",
                OK if good else WARN,
                f"{u2_version}",
                None if good else "3.7 or newer is required; older releases install an APK.",
            )
        )
    except ImportError:
        rows.append(
            _row("uiautomator2", FAIL, "not importable",
                 "The plugin environment is incomplete; rerun /jev-hands:doctor after uv syncs.")
        )

    # 7. adb
    adb_path = android_u2.find_adb()
    if adb_path:
        rows.append(_row("adb", OK, f"{adb_path} ({android_u2.adb_version(adb_path) or 'version unknown'})"))
    else:
        rows.append(_row("adb", FAIL, "not found", android_u2.ADB_INSTALL_HINT))

    # 8. device link, skippable
    if skip_device_probe(check_device):
        rows.append(
            _row("device", SKIPPED, f"device probe disabled ({SKIP_DEVICE_ENV} or check_device=false)")
        )
    elif not adb_path:
        rows.append(_row("device", SKIPPED, "cannot look for devices without adb"))
    else:
        rows.extend(_device_rows(serial))

    rows.append(
        _row(
            "phone_footprint",
            OK,
            f"uiautomator2 pushes one file, {android_u2.PUSHED_ARTIFACT}, and installs no APK",
            f"To remove it: adb -s <serial> shell rm {android_u2.PUSHED_ARTIFACT}",
        )
    )

    failures = [r for r in rows if r["status"] == FAIL]
    warnings = [r for r in rows if r["status"] == WARN]
    if failures:
        summary = (
            f"{len(failures)} check(s) need fixing: "
            + ", ".join(r["check"] for r in failures)
        )
    elif warnings:
        summary = "Ready, with notes on: " + ", ".join(r["check"] for r in warnings)
    else:
        summary = "Everything checks out."

    return {
        "ok": not failures,
        "summary": summary,
        "checks": rows,
        "config": config_sources(),
    }


def _device_rows(serial: Optional[str]) -> List[Dict[str, Any]]:
    from .errors import JevHandsError

    rows: List[Dict[str, Any]] = []
    try:
        devices = android_u2.list_devices()
    except JevHandsError as exc:
        return [_row("device", FAIL, exc.message, exc.hint)]

    if not devices:
        return [
            _row("device", FAIL, "no device attached",
                 "Plug the phone in over USB and turn on USB debugging.")
        ]

    ready = [d for d in devices if d["state"] == "device"]
    unauthorized = [d for d in devices if d["state"] == "unauthorized"]
    if unauthorized:
        rows.append(
            _row("device", FAIL, f"{len(unauthorized)} device(s) unauthorized",
                 "Unlock the phone and tap Allow on the USB debugging prompt.")
        )
    if not ready:
        return rows
    if len(ready) > 1 and not serial:
        rows.append(
            _row("device", WARN, f"{len(ready)} devices attached",
                 "Pass a serial to jev_device(action='connect', serial=...).")
        )
        return rows

    chosen = serial or ready[0]["serial"]
    adapter = android_u2.AndroidAdapter(chosen)
    try:
        info = adapter.connect()
    except JevHandsError as exc:
        rows.append(_row("device", FAIL, exc.message, exc.hint))
        return rows
    rows.append(_row("device", OK, "one device ready and the uiautomator2 agent responded"))
    if info.get("screen_on") is False:
        rows.append(_row("screen", WARN, "the screen is off", "Wake and unlock the phone."))
    else:
        rows.append(_row("screen", OK, "the screen is on"))
    return rows


def _package_version(name: str) -> Optional[str]:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:  # noqa: BLE001 - metadata is best effort
        return None


def _version_at_least(raw: str, minimum: tuple) -> bool:
    parts = []
    for chunk in str(raw).split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts[: len(minimum)]) >= minimum


def as_text(report: Dict[str, Any]) -> str:
    """Plain text rendering for the slash command."""
    lines = [report["summary"], ""]
    for row in report["checks"]:
        line = f"{row['check']:<18} {row['status']:<8} {row['detail']}"
        lines.append(line)
        if row.get("fix"):
            lines.append(f"{'':<18} {'fix':<8} {row['fix']}")
    return "\n".join(lines)


def _main(argv: Optional[List[str]] = None) -> int:
    """`python -m jev_hands.doctor` for the slash command."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Check the jev-hands environment.")
    parser.add_argument("--text", action="store_true", help="plain text instead of JSON")
    parser.add_argument("--no-device", action="store_true", help="never touch the phone")
    args = parser.parse_args(argv)

    report = run(check_device=False if args.no_device else None)
    print(as_text(report) if args.text else json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
