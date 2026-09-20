"""Read the TypeSafe API key.

Order: the ``TYPESAFE_API_KEY`` environment variable (a development and CI
override), then the macOS Keychain.

This module exposes exactly three functions: ``get()``, ``exists()`` and
``source()``. There is deliberately no way to print the value. The key is never
put on a command line: reads go through ``security find-generic-password``,
whose output is captured by this process and nothing else.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Optional, Tuple

LOG = logging.getLogger(__name__)

SERVICE = "jev-hands"
ACCOUNT = "typesafe-api-key"
ENV_VAR = "TYPESAFE_API_KEY"
SECURITY_BIN = "/usr/bin/security"
SECURITY_TIMEOUT = 10.0

# `security` uses this for errSecItemNotFound. Any other non-zero code is a real
# failure (locked keychain, denied authorisation) and must not be reported as
# "no key stored".
EXIT_ITEM_NOT_FOUND = 44

SOURCE_ENV = "env"
SOURCE_KEYCHAIN = "keychain"
SOURCE_NONE = "none"
SOURCE_UNSUPPORTED = "unsupported_platform"

_cache: Optional[str] = None
_cache_source: Optional[str] = None


def is_macos() -> bool:
    return sys.platform == "darwin"


def _env_value() -> Optional[str]:
    import os

    value = os.environ.get(ENV_VAR)
    if value and value.strip():
        return value.strip()
    return None


def _run_security(args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [SECURITY_BIN, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=SECURITY_TIMEOUT,
    )


def _keychain_exists() -> bool:
    """Existence check without pulling the plaintext into this process."""
    if not is_macos():
        return False
    try:
        proc = _run_security(["find-generic-password", "-s", SERVICE, "-a", ACCOUNT])
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.warning("keychain lookup failed: %s", type(exc).__name__)
        return False
    if proc.returncode == 0:
        return True
    if proc.returncode == EXIT_ITEM_NOT_FOUND:
        return False
    LOG.warning("keychain lookup returned exit code %d", proc.returncode)
    return False


def _keychain_get() -> Optional[str]:
    if not is_macos():
        return None
    try:
        proc = _run_security(["find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.warning("keychain read failed: %s", type(exc).__name__)
        return None
    if proc.returncode != 0:
        if proc.returncode != EXIT_ITEM_NOT_FOUND:
            LOG.warning("keychain read returned exit code %d", proc.returncode)
        return None
    value = proc.stdout.strip()
    return value or None


def source() -> str:
    """Where the key would come from, without reading it.

    One of ``env``, ``keychain``, ``none`` or ``unsupported_platform``.
    """
    if _env_value():
        return SOURCE_ENV
    if not is_macos():
        return SOURCE_UNSUPPORTED
    return SOURCE_KEYCHAIN if _keychain_exists() else SOURCE_NONE


def exists() -> Tuple[bool, str]:
    """(is a key configured, where it comes from). Never touches the value on
    the keychain path."""
    found = source()
    return (found in (SOURCE_ENV, SOURCE_KEYCHAIN), found)


def get() -> Optional[str]:
    """The key itself, for the Authorization header only.

    Cached for the life of the process: an MCP server is long lived and hitting
    the keychain on every tool call is both slow and noisy.
    """
    global _cache, _cache_source
    if _cache is not None:
        return _cache

    value = _env_value()
    if value:
        _cache, _cache_source = value, SOURCE_ENV
        return _cache

    value = _keychain_get()
    if value:
        _cache, _cache_source = value, SOURCE_KEYCHAIN
        return _cache

    return None


def cached_source() -> Optional[str]:
    return _cache_source


def reset_cache() -> None:
    """Used by the tests and after /jev-hands:setup stores a new key."""
    global _cache, _cache_source
    _cache = None
    _cache_source = None
