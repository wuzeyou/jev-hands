---
description: Check the jev-hands environment and report the fixes
allowed-tools: Bash
---

Run the jev-hands environment check and present the report.

This command takes no arguments. If the user typed anything after the command
name, ignore it, and say once that it was ignored. Never splice user text into a
shell command.

## 1. uv first

Everything else depends on it:

```
command -v uv || echo MISSING_UV
```

On `MISSING_UV`, stop here. Tell the user the MCP server is started through uv,
so nothing works until it is installed:

- macOS and Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Windows: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`

Then have them start a fresh Claude Code session, because the server needs uv at
start-up.

## 2. Run the check

```
UV_PROJECT_ENVIRONMENT="$({ D="${CLAUDE_PLUGIN_DATA}"; [ -n "$D" ] || D="$HOME/.jev-hands"; echo "$D/venv"; })" \
  uv run --project "${CLAUDE_PLUGIN_ROOT}" --no-dev --frozen python -m jev_hands.doctor --text
```

The first run may spend a while building the plugin environment. When it does,
finish by telling the user to reconnect `jev-hands-android` in `/mcp`: the server
may have timed out on its first start.

Add `--no-device` to the same line to check everything except the phone. Use it
when the user says the phone is not plugged in, or when a device probe is
hanging.

## 3. Present it

Show the report as it came out. Do not rewrite it, do not summarise it, do not
add advice of your own: every row already carries its own fix line. The user runs
the fixes themselves unless they ask you to.

The rows are: `platform`, `python`, `uv`, `data_dir`, `api_key`, `uiautomator2`,
`adb`, `device`, `screen`, `phone_footprint`.

Two rows deserve a sentence of context when they are not `ok`:

- `api_key` says `not configured` -> the fix is `/jev-hands:setup`. It reports
  presence and origin only; it never reads the value.
- `device` says `unauthorized` -> the user has to unlock the phone and accept the
  USB debugging prompt on the device itself. Nothing on this machine can do it
  for them.
