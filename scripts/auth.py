#!/usr/bin/env python3
"""Store the TypeSafe API key in the macOS keychain.

The value is typed into a hidden system dialog, comes back on the dialog's
stdout into this process's memory, and goes straight into `security` through
stdin. It never reaches a command line, a terminal, a log or the conversation.

Subcommands: `status` (is one configured, and where from), `set` (write or
rotate), `clear` (remove). There is deliberately no subcommand that prints the
value.

Exit codes:
  0  success
  1  already exists (use --force) or nothing found to remove
  2  operation failed: cancelled, timed out, keychain locked, not macOS, bad value
  3  a dependency is not ready
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402
import subprocess  # noqa: E402

SERVICE = "jev-hands"
ACCOUNT = "typesafe-api-key"
KIND = "api-key"
DESCRIPTION = "TypeSafe Jev API key (jev-hands plugin)"
ENV_VAR = "TYPESAFE_API_KEY"

SECURITY_BIN = "/usr/bin/security"
SECURITY_TIMEOUT = 10.0
DIALOG_TIMEOUT = 120.0
EXIT_ITEM_NOT_FOUND = 44

EXIT_OK = 0
EXIT_STATE = 1
EXIT_FAILED = 2
EXIT_DEPENDENCY = 3


class AuthError(Exception):
    pass


def _fail(message: str) -> None:
    print(f"FAILED: {message}")


def _quote(field: str, label: str) -> str:
    """Escape a field going inside double quotes of a `security -i` command.

    `security -i` parses one line at a time, so a newline or control character
    cannot be represented safely; reject those outright.
    """
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in field):
        raise AuthError(f"{label} contains a newline or control character")
    return field.replace("\\", "\\\\").replace('"', '\\"')


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise AuthError(
            f"jev-hands phase 1 stores credentials on macOS only; this is {sys.platform}"
        )


def _run_security(args, stdin_text=None) -> subprocess.CompletedProcess:
    """Every call gets a timeout: a locked keychain makes `security` wait for an
    authorisation dialog forever."""
    try:
        return subprocess.run(
            [SECURITY_BIN, *args],
            input=stdin_text,
            stdin=None if stdin_text is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=SECURITY_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise AuthError("the `security` tool is missing; this needs macOS") from exc
    except subprocess.TimeoutExpired as exc:
        raise AuthError(
            "the keychain did not answer in 10s; it may be locked or waiting for "
            "an authorisation prompt"
        ) from exc


def keychain_has_key() -> bool:
    """Existence check that never pulls the value into this process."""
    proc = _run_security(["find-generic-password", "-s", SERVICE, "-a", ACCOUNT])
    if proc.returncode == 0:
        return True
    if proc.returncode == EXIT_ITEM_NOT_FOUND:
        return False
    raise AuthError(f"the keychain lookup failed with exit code {proc.returncode}")


def prompt_hidden_value() -> str:
    """Ask for the value in a hidden system dialog.

    The dialog text is a fixed string, so there is nothing to inject. The value
    arrives on osascript's stdout, is captured here, and is never printed.
    """
    script = (
        'display dialog "Paste the TypeSafe API key for jev-hands:" & return & return & '
        '"Input is hidden. The value goes straight into the macOS keychain, '
        'not through the terminal, the assistant, or any log." '
        'default answer "" with hidden answer '
        'buttons {"Cancel","Save"} default button "Save" '
        'with title "jev-hands"'
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script, "-e", "text returned of result"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=DIALOG_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise AuthError("osascript is unavailable; this needs a macOS GUI session") from exc
    except subprocess.TimeoutExpired as exc:
        raise AuthError("the dialog timed out") from exc

    if proc.returncode != 0:
        raise AuthError("the dialog was cancelled or could not open")

    value = proc.stdout.strip()
    if not value:
        raise AuthError("nothing was entered; cancelled")
    if not value.isascii():
        raise AuthError("the key must be ASCII; the keychain returns non-ASCII values as hex")
    return value


def write_key(value: str) -> None:
    """Hand the value to `security` over stdin so it stays out of argv."""
    command = (
        "add-generic-password -U"
        f' -s "{_quote(SERVICE, "service")}"'
        f' -a "{_quote(ACCOUNT, "account")}"'
        f' -D "{_quote(KIND, "kind")}"'
        f' -j "{_quote(DESCRIPTION, "description")}"'
        f' -w "{_quote(value, "value")}"'
    )
    proc = _run_security(["-i"], stdin_text=command + "\n")
    if proc.returncode != 0:
        raise AuthError(f"the keychain write failed with exit code {proc.returncode}")


def cmd_status() -> int:
    import os

    if os.environ.get(ENV_VAR, "").strip():
        print(f"CONFIGURED: {ENV_VAR} environment variable (overrides the keychain)")
        return EXIT_OK
    _require_macos()
    if keychain_has_key():
        print(f"CONFIGURED: macOS keychain (service {SERVICE}, account {ACCOUNT})")
        return EXIT_OK
    print("NOT_CONFIGURED")
    return EXIT_STATE


def cmd_set(force: bool) -> int:
    _require_macos()
    if keychain_has_key() and not force:
        print("ALREADY_EXISTS: a key is already stored; rerun with --force to rotate it")
        return EXIT_STATE
    value = prompt_hidden_value()
    try:
        write_key(value)
    finally:
        del value
    print("OK: stored in the macOS keychain")
    return EXIT_OK


def cmd_clear() -> int:
    _require_macos()
    proc = _run_security(["delete-generic-password", "-s", SERVICE, "-a", ACCOUNT])
    if proc.returncode == 0:
        print("OK: removed from the macOS keychain")
        return EXIT_OK
    if proc.returncode == EXIT_ITEM_NOT_FOUND:
        print("NOT_FOUND: nothing stored")
        return EXIT_STATE
    raise AuthError(f"the keychain delete failed with exit code {proc.returncode}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Manage the jev-hands API key.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="report whether a key is configured and where from")
    set_parser = sub.add_parser("set", help="store or rotate the key using a hidden dialog")
    set_parser.add_argument("--force", action="store_true", help="overwrite an existing key")
    sub.add_parser("clear", help="remove the stored key")

    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            return cmd_status()
        if args.command == "set":
            return cmd_set(args.force)
        if args.command == "clear":
            return cmd_clear()
    except AuthError as exc:
        _fail(str(exc))
        return EXIT_FAILED
    except ImportError as exc:  # pragma: no cover - environment problem
        _fail(f"a dependency is not ready: {type(exc).__name__}")
        return EXIT_DEPENDENCY
    return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
