"""Android adapter over uiautomator2 3.7 or newer.

Two rules that came out of a round of real-device testing and must not be
relaxed:

* Text is typed with ``set_text`` on the focused node. The clipboard based
  helper throws a SecurityException on recent Android and then tries to install
  an input-method APK, which vendor security software blocks outright.
* uiautomator2 3.7+ pushes one jar to ``/data/local/tmp`` and installs no APK.
  Older releases do install one, hence the version floor.
* Some windows never reach the accessibility tree at all: secure windows -
  biometric prompts, system password and PIN entry, payment keyboards - and
  apps that switch their own tree off. The dump succeeds and describes nothing,
  which is why an empty result is classified rather than reported as a failed
  read. See ``candidates.unreadable_reason``.

Every failure leaves this module as a structured error with a remediation hint,
never as a bare exception.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import candidates as candidates_mod
from ..core.models import (
    ACTION_BACK,
    ACTION_SCROLL_DOWN,
    ACTION_SCROLL_UP,
    ACTION_TAP,
    ACTION_TYPE,
    OBSERVE_DUMP_ERROR,
    OBSERVE_FAILED_HINTS,
    Screen,
)
from ..errors import DeviceError, JevHandsError, ObserveError

LOG = logging.getLogger(__name__)

ADB_TIMEOUT = 15.0
CONNECT_ATTEMPTS = 3
CONNECT_GAP_SECONDS = 2.0
PUSHED_ARTIFACT = "/data/local/tmp/u2.jar"

COMMON_ADB_DIRS = (
    "~/Library/Android/sdk/platform-tools",
    "~/Android/Sdk/platform-tools",
    "/usr/local/share/android-sdk/platform-tools",
    "/opt/homebrew/share/android-commandlinetools/platform-tools",
    "/opt/android-sdk/platform-tools",
)

# Sentinel for "not looked up yet", so that a lookup that legitimately found
# nothing is not repeated on every observation.
_UNSET = object()

ADB_INSTALL_HINT = (
    "Install the Android platform tools, for example `brew install --cask "
    "android-platform-tools`, or set ANDROID_HOME to an existing SDK."
)


def find_adb() -> Optional[str]:
    """PATH, then ANDROID_HOME, then ANDROID_SDK_ROOT, then the usual places."""
    on_path = shutil.which("adb")
    if on_path:
        return on_path
    for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(variable)
        if not root:
            continue
        candidate = Path(root).expanduser() / "platform-tools" / "adb"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    for directory in COMMON_ADB_DIRS:
        candidate = Path(directory).expanduser() / "adb"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _run_adb(adb: str, args: List[str], *, timeout: float = ADB_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [adb, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def adb_version(adb: Optional[str] = None) -> Optional[str]:
    binary = adb or find_adb()
    if not binary:
        return None
    try:
        proc = _run_adb(binary, ["version"], timeout=10.0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else None


def list_devices(adb: Optional[str] = None) -> List[Dict[str, str]]:
    """Every attached device with its state. Raises DeviceError when adb is
    missing or will not run."""
    binary = adb or find_adb()
    if not binary:
        raise DeviceError(
            "adb was not found on this machine.",
            code="adb_missing",
            hint=ADB_INSTALL_HINT,
        )
    try:
        proc = _run_adb(binary, ["devices", "-l"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeviceError(
            f"adb did not respond ({type(exc).__name__}).",
            code="adb_unresponsive",
            hint="Unplug and replug the phone, then run /jev-hands:doctor again.",
        ) from exc
    if proc.returncode != 0:
        raise DeviceError(
            "adb could not list devices.",
            code="adb_failed",
            hint="Check the USB cable and that USB debugging is on.",
        )

    devices: List[Dict[str, str]] = []
    for line in proc.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        entry = {"serial": parts[0], "state": parts[1]}
        for extra in parts[2:]:
            if ":" in extra:
                key, _, value = extra.partition(":")
                entry[key] = value
        devices.append(entry)
    return devices


def select_serial(serial: Optional[str] = None, adb: Optional[str] = None) -> str:
    """One device selects itself. More than one needs an explicit serial."""
    devices = list_devices(adb)
    if serial:
        for device in devices:
            if device["serial"] == serial:
                if device["state"] != "device":
                    raise DeviceError(
                        f"The selected device is in state '{device['state']}'.",
                        code="device_not_ready",
                        hint=(
                            "If it says 'unauthorized', unlock the phone and accept the "
                            "USB debugging prompt."
                        ),
                    )
                return serial
        raise DeviceError(
            "That serial is not attached.",
            code="device_not_found",
            hint="Run jev_device(action='list') to see what is attached.",
            details={"devices": devices},
        )

    ready = [d for d in devices if d["state"] == "device"]
    if not ready:
        unauthorized = [d for d in devices if d["state"] == "unauthorized"]
        if unauthorized:
            raise DeviceError(
                "The phone is attached but not authorised for debugging.",
                code="device_unauthorized",
                hint="Unlock the phone and tap Allow on the USB debugging prompt.",
                details={"devices": devices},
            )
        raise DeviceError(
            "No Android device is attached.",
            code="no_device",
            hint="Plug the phone in over USB and turn on USB debugging.",
            details={"devices": devices},
        )
    if len(ready) > 1:
        raise DeviceError(
            "More than one device is attached; pass a serial.",
            code="ambiguous_device",
            hint="Call jev_device(action='connect', serial=...) with one of the listed serials.",
            details={"devices": ready},
        )
    return ready[0]["serial"]


class AndroidAdapter:
    """observe / fresh / execute against one phone."""

    name = "android"

    def __init__(self, serial: str, *, adb: Optional[str] = None) -> None:
        self.serial = serial
        self.adb = adb or find_adb()
        self._device: Any = None
        self._last_fingerprint: Optional[str] = None
        self._last_capture_id: Optional[str] = None
        self._ime_package: Any = _UNSET

    # -- connection ------------------------------------------------------------
    def connect(self) -> Dict[str, Any]:
        """Connect, retrying three times two seconds apart.

        The agent on the phone runs as the shell user and the system can kill
        it, so a reconnect is normal rather than exceptional.
        """
        try:
            import uiautomator2  # noqa: WPS433 - optional heavy import
        except ImportError as exc:
            raise DeviceError(
                "The uiautomator2 package is not installed in the server environment.",
                code="uiautomator2_missing",
                hint="Run /jev-hands:doctor to rebuild the plugin environment.",
            ) from exc

        last: Optional[Exception] = None
        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            try:
                self._device = uiautomator2.connect(self.serial)
                info = {}
                try:
                    info = dict(self._device.info or {})
                except Exception:  # pragma: no cover - device specific
                    info = {}
                return {
                    "serial": self.serial,
                    "connected": True,
                    "attempt": attempt,
                    "pushed": [PUSHED_ARTIFACT],
                    "screen_on": info.get("screenOn"),
                }
            except Exception as exc:  # noqa: BLE001 - uiautomator2 raises many types
                last = exc
                LOG.warning(
                    "uiautomator2 connect attempt %d/%d failed: %s",
                    attempt,
                    CONNECT_ATTEMPTS,
                    type(exc).__name__,
                )
                if attempt < CONNECT_ATTEMPTS:
                    time.sleep(CONNECT_GAP_SECONDS)

        raise DeviceError(
            f"Could not reach the uiautomator2 agent on the phone ({type(last).__name__}).",
            code="u2_connect_failed",
            hint=(
                "Unlock the screen and retry. The agent runs as the shell user and the "
                "system can kill it; /jev-hands:doctor re-initialises it."
            ),
        )

    def device(self) -> Any:
        if self._device is None:
            self.connect()
        return self._device

    def _reconnect(self) -> None:
        self._device = None
        self.connect()

    # -- observe ---------------------------------------------------------------
    def current_app(self) -> Dict[str, Optional[str]]:
        try:
            info = self.device().app_current() or {}
        except Exception as exc:  # noqa: BLE001
            LOG.warning("app_current failed: %s", type(exc).__name__)
            return {"package": None, "activity": None}
        return {"package": info.get("package"), "activity": info.get("activity")}

    def default_ime_package(self) -> Optional[str]:
        """The package of the phone's current input method, looked up once.

        `settings get secure default_input_method` answers
        `<package>/<service>`. It is read at most once per adapter and any
        failure is cached as "unknown", after which the fingerprint falls back
        to recognising input methods by their package name alone.
        """
        if self._ime_package is not _UNSET:
            return self._ime_package
        self._ime_package = None
        if not self.adb:
            return None
        args = ["-s", self.serial, "shell", "settings", "get", "secure", "default_input_method"]
        try:
            proc = _run_adb(self.adb, args, timeout=10.0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.warning("reading the default input method failed: %s", type(exc).__name__)
            return None
        if proc.returncode != 0:
            return None
        raw = (proc.stdout or "").strip()
        if not raw or raw == "null" or "/" not in raw:
            return None
        self._ime_package = raw.split("/", 1)[0] or None
        return self._ime_package

    def dump(self) -> str:
        """The raw hierarchy, with one reconnect in between.

        A dump that raises is `dump_error`: the read itself did not happen. A
        dump that comes back describing nothing is a different thing entirely -
        a secure window or an app hiding its tree - and is classified in
        `candidates.unreadable_reason`, not here.
        """
        last: Optional[Exception] = None
        for attempt in range(1, 3):
            try:
                return self.device().dump_hierarchy()
            except Exception as exc:  # noqa: BLE001
                last = exc
                LOG.warning("dump_hierarchy attempt %d failed: %s", attempt, type(exc).__name__)
                self._reconnect()
        raise ObserveError(
            f"The UI tree could not be read ({type(last).__name__}).",
            hint=OBSERVE_FAILED_HINTS[OBSERVE_DUMP_ERROR],
            details={"reason": OBSERVE_DUMP_ERROR, "serial_known": True},
        )

    def screenshot(self, directory: Path) -> Optional[str]:
        """One PNG in `directory`, or None. Never fatal.

        A secure window comes out black or as the app behind it: Android blocks
        the capture the same way it hides the tree.
        """
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"screen-{int(time.time() * 1000)}.png"
            self.device().screenshot(str(path))
            return str(path)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("screenshot failed: %s", type(exc).__name__)
            return None

    def observe(
        self,
        *,
        token_budget: int = 1500,
        scroll: Optional[Dict[str, bool]] = None,
        screenshot: bool = False,
        screenshot_dir: Optional[Path] = None,
    ) -> Screen:
        app = self.current_app()
        xml_text = self.dump()
        screen = candidates_mod.build_screen(
            xml_text,
            package=app.get("package"),
            activity=app.get("activity"),
            token_budget=token_budget,
            scroll=scroll,
            ime_package=self.default_ime_package(),
        )
        if screenshot and screenshot_dir is not None:
            screen.screenshot_path = self.screenshot(screenshot_dir)
        self._last_fingerprint = screen.fingerprint
        self._last_capture_id = screen.capture_id
        return screen

    def fresh(self, capture_id: str) -> bool:
        if capture_id != self._last_capture_id:
            return False
        try:
            xml_text = self.dump()
        except JevHandsError:
            return False
        root = candidates_mod.parse_hierarchy(xml_text)
        # Scope the digest exactly the way the observation did. The fingerprint
        # depends on the tree alone - no package is passed in from outside -
        # so the two sides cannot drift apart, and a new window from any
        # package shows up as a change.
        return (
            candidates_mod.fingerprint(root, ime_package=self.default_ime_package())
            == self._last_fingerprint
        )

    # -- execute ---------------------------------------------------------------
    def execute(self, action: Dict[str, Any]) -> Dict[str, Any]:
        kind = action.get("type")
        device = self.device()
        try:
            if kind == ACTION_TAP:
                device.click(int(action["x"]), int(action["y"]))
                return {"type": kind, "at": [int(action["x"]), int(action["y"])]}

            if kind == ACTION_TYPE:
                text = action.get("text") or ""
                if "x" in action and "y" in action:
                    device.click(int(action["x"]), int(action["y"]))
                    time.sleep(0.2)
                # set_text goes through the accessibility ACTION_SET_TEXT path.
                # It handles non-ASCII and leaves the input method alone.
                device(focused=True).set_text(text)
                return {"type": kind, "chars": len(text)}

            if kind in (ACTION_SCROLL_DOWN, ACTION_SCROLL_UP):
                width, height = device.window_size()
                x = width // 2
                if kind == ACTION_SCROLL_DOWN:
                    device.swipe(x, int(height * 0.72), x, int(height * 0.28), 0.2)
                else:
                    device.swipe(x, int(height * 0.28), x, int(height * 0.72), 0.2)
                return {"type": kind}

            if kind == ACTION_BACK:
                device.press("back")
                return {"type": kind}

            if kind == "press":
                device.press(str(action.get("key", "home")))
                return {"type": kind, "key": action.get("key")}

        except Exception as exc:  # noqa: BLE001
            # A write is never retried: we cannot tell whether it landed.
            raise JevHandsError(
                f"The action may or may not have landed ({type(exc).__name__}).",
                code="execute_uncertain",
                hint="Observe again before doing anything else. Never repeat a write blindly.",
            ) from exc

        raise JevHandsError(
            f"Unknown action type {kind!r}.",
            code="bad_action",
            hint="Use one of tap_element, type_text, scroll_down, scroll_up, go_back, press.",
        )

    def resolve_launcher_activity(self, package: str) -> Optional[str]:
        """Ask the package manager which activity the launcher would start.

        `am start` with a bare package name treats it as intent data, not as a
        component, and silently starts nothing useful, so the component has to
        be resolved before starting it.
        """
        if not self.adb:
            return None
        args = ["-s", self.serial, "shell", "cmd", "package", "resolve-activity", "--brief", package]
        try:
            proc = _run_adb(self.adb, args, timeout=20.0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.warning("resolving the launcher activity failed: %s", type(exc).__name__)
            return None
        if proc.returncode != 0:
            return None
        prefix = package + "/"
        for line in reversed((proc.stdout or "").splitlines()):
            candidate = line.strip()
            if candidate.startswith(prefix):
                return candidate[len(prefix):] or None
        return None

    def launch(self, package: str, activity: Optional[str] = None) -> Dict[str, Any]:
        """Start an app.

        Without an activity the launcher component is resolved through the
        package manager and started with `am start -n`, which is unambiguous.
        `monkey` is deliberately avoided: some apps never come up under it, and
        uiautomator2's own `app_start()` falls back to it whenever no activity
        is given. It is still the last resort here, for the rare package the
        package manager will not resolve.
        """
        via = "am_start"
        if activity is None:
            activity = self.resolve_launcher_activity(package)
            via = "am_start_resolved"

        if activity is None:
            try:
                self.device().app_start(package)
            except Exception as exc:  # noqa: BLE001 - uiautomator2 raises many types
                raise DeviceError(
                    f"The app did not start ({type(exc).__name__}).",
                    code="launch_failed",
                    hint=(
                        "Check the package name with `adb shell pm list packages`. Some "
                        "launchers show a chooser first; observe the screen and pick an entry."
                    ),
                ) from exc
            return {"package": package, "activity": None, "via": "app_start"}

        if not self.adb:
            raise DeviceError("adb was not found.", code="adb_missing", hint=ADB_INSTALL_HINT)
        args = ["-s", self.serial, "shell", "am", "start", "-n", f"{package}/{activity}"]
        try:
            proc = _run_adb(self.adb, args, timeout=20.0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeviceError(
                f"Launching the app timed out ({type(exc).__name__}).",
                code="launch_failed",
                hint="Unlock the phone and try again.",
            ) from exc
        if proc.returncode != 0 or "Error" in (proc.stderr or ""):
            raise DeviceError(
                "The app did not start.",
                code="launch_failed",
                hint=(
                    "Check the package and activity names. Some launchers show a chooser "
                    "first; observe the screen and pick an entry."
                ),
                details={"stderr": (proc.stderr or "").strip()[:400]},
            )
        return {"package": package, "activity": activity, "via": via}
