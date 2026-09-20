# Decomposition tables

Two tables from `SKILL.md`, word for word: who owns each type of step, and how
to read a stop as a diagnosis of the plan. The method they belong to, and the
rest of the decomposition section, is in `SKILL.md`.

### Step types and who owns them

| Type | What it is | Owner | How |
|---|---|---|---|
| Navigate | Move to a screen where something is visible | Model | `jev_run` with `goal` and `end_state` |
| Select by criteria | Pick one of many similar items using the user's intent | Planner | `jev_observe`, filter the candidate table, `jev_act`; or ask the user |
| Enter text | Put a value into a field | Planner supplies, model targets | `jev_run` with `text_to_type`; submitting is its own navigate step |
| Verify | Confirm the screen is in the expected state | Planner | `jev_check`, read the candidate table |
| Handle interruption | Choosers, adverts, permission requests, verification challenges | Planner; challenges go to the human | `jev_observe`, `jev_act`, or wait for the person |
| Commit | An action with consequences outside the screen | User, or planner only with explicit permission | `jev_act` after confirmation, or stop and report |

The split is by who holds the information the step needs, not by how hard the
tap is.

### Read a stop as a diagnosis

Each stop reason says which assumption broke.

| What came back | What it says about the plan | Adjust |
|---|---|---|
| `reached` stays low although you believe you are there | The end_state describes a route, not a destination | Rewrite it as visible content |
| `ask_low_confidence` with two close candidates | The goal is ambiguous on this screen, or it is really a decision point | Name the visible label; or decide it yourself from the table |
| `stop_none` | The target is not on this screen | Scroll, go back, or the route is wrong |
| `unchanged_twice` | The interaction model is wrong: a different action is needed (a button, not enter) | Change the action, not the wording |
| `max_steps` | The sub-goal is too coarse | Split at an intermediate visible state |
| `ask_popup` | An interruption the plan did not expect | Handle it, then add it to the plan |
| `observe_failed` | The screen cannot be read as text | Screenshot, act directly, or route around it |
| `stale_screen` again and again | The page never settles | Wait, then screenshot |
