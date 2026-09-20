---
description: Store the TypeSafe API key in the system keychain
allowed-tools: Bash, AskUserQuestion
---

Get the user's `TYPESAFE_API_KEY` into the macOS keychain. This is the only
credential jev-hands needs.

Ignore any arguments the user typed after the command name, and say once that
they were ignored. Never paste user-supplied text into a shell command here.

## 1. Look before asking

Run:

```
UV_PROJECT_ENVIRONMENT="$({ D="${CLAUDE_PLUGIN_DATA}"; [ -n "$D" ] || D="$HOME/.jev-hands"; echo "$D/venv"; })" \
  uv run --project "${CLAUDE_PLUGIN_ROOT}" --no-dev --frozen python "${CLAUDE_PLUGIN_ROOT}/scripts/auth.py" status
```

The output is one of `CONFIGURED: <origin>`, `NOT_CONFIGURED`, or `FAILED: <reason>`.
It never contains the key itself.

- This first run may take a while: uv builds the plugin environment. If it did,
  tell the user plainly at the end to reconnect `jev-hands-android` in `/mcp`,
  because the server may have timed out on its first start.
- `CONFIGURED` -> say where it is stored, then ask with AskUserQuestion whether to
  rotate it. Options: `Keep the current key (recommended)` and
  `Replace it with a new one`. On "keep", stop here.
- Exit code 3 -> read out the real reason from stderr and send the user to
  `/jev-hands:doctor`.
- `FAILED` -> read the reason out as-is and stop. Do not work around it.

## 2. Confirm before writing

Always ask once with AskUserQuestion before storing anything, even when the user
already said to go ahead. Lead with the recommended option.

Question: "Open the system dialog now and store the TypeSafe API key?"
Options:
- `Store it now (recommended)` - a macOS dialog opens, input is hidden, the value
  goes straight into the keychain under service `jev-hands`, account
  `typesafe-api-key`. It does not pass through this conversation, the terminal,
  a command line or any log.
- `I use the TYPESAFE_API_KEY environment variable` - nothing is stored; explain
  that the environment variable wins over the keychain and that
  `/jev-hands:doctor` will report the origin as `env`.
- `Not now` - stop.

If the machine is not macOS, do not run the dialog. Say that phase 1 stores
credentials on macOS only, and that on other platforms the only supported route
today is exporting `TYPESAFE_API_KEY` in the user's own shell.

## 3. Write it

On "store it now", run (add `--force` when rotating an existing key):

```
UV_PROJECT_ENVIRONMENT="$({ D="${CLAUDE_PLUGIN_DATA}"; [ -n "$D" ] || D="$HOME/.jev-hands"; echo "$D/venv"; })" \
  uv run --project "${CLAUDE_PLUGIN_ROOT}" --no-dev --frozen python "${CLAUDE_PLUGIN_ROOT}/scripts/auth.py" set
```

A hidden macOS dialog appears. The script prints only a status line:
`OK: stored in the macOS keychain`, or `FAILED: ...` for a cancelled dialog, a
timeout, a locked keychain or a rejected value.

## 4. Check and hand over

Rerun the `status` command from step 1. On `CONFIGURED`, tell the user two things:

1. Where the key lives, and that jev-hands only ever reads it into the
   `Authorization` header.
2. If the MCP server started before the key existed, reconnect
   `jev-hands-android` in `/mcp`, then run `/jev-hands:doctor`.

## Rules that outrank the task

- Never ask the user to paste the key into this conversation. The hidden dialog
  is the only channel. If they paste one anyway: stop, tell them the value is now
  in the session transcript, have them revoke it in the TypeSafe console, issue a
  new one, and start this command over.
- Never put the value in a shell argument, a comment, a config file, `.mcp.json`,
  or any output.
- Never read the key back "to confirm". Existence is the only thing to check.
