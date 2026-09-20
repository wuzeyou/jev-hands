# jev-hands

A Claude Code plugin that lets Claude drive an Android phone.

On every screen the plugin reads the accessibility tree, turns it into a short
numbered list of candidates, and asks [TypeSafe's Jev](https://docs.typesafe.ai)
- a text-only decision model - which one to act on. Jev answers with an index
and a probability. The plugin converts the index to coordinates, validates the
answer, checks it against a confidence policy, and only then touches the
phone.

The point is speed and cost. A decision takes about 0.7 seconds and a fraction
of a cent, against several seconds and thousands of tokens for a screenshot
round trip. Claude stays in charge of planning; Jev only answers "which one".

The plugin knows nothing about any particular app. Ordering food, checking a
parcel and so on are written as skills on top of it.

## What counts as the screen

Everything drawn on top of the app is part of the screen. A permission prompt, a
vendor app-clone chooser, a share sheet - all of them are separate windows
belonging to other packages, and their buttons go into the candidate table like
any other element, with the topmost window's rows at the head of the table so
they survive the token budget. Three kinds of window are ignored: the system UI
(a clock that ticks every second), the input method, and edge strips such as the
gesture bar with nothing to interact with on them. `app.windows` lists every
window that was read, so a planner can see that something is sitting over the
app; when more than one package is drawing, each row also names its own window.

## Secure screens

Android keeps some windows out of the accessibility tree entirely: biometric
prompts, the system password and PIN entry, payment keyboards, and any app that
switches its own tree off. The window is there, the dump succeeds, and what
comes back is a window with no class, no size and nothing under it. A screenshot
of one of those is often black as well, so there is no picture to fall back on
either.

The plugin says so rather than handing over an empty candidate table: the
observation carries `unreadable: hidden_tree`, and a step ends as
`observe_failed` with `reason: hidden_tree` and a hint saying to hand over to
the person holding the phone. The other two reasons are `empty_tree` (the dump
held nothing at all) and `dump_error` (the read itself failed - the phone
asleep, the agent killed). Only `dump_error` is worth retrying.

## Requirements

- macOS. Phase 1 is macOS only; on other platforms the server starts but every
  tool returns `unsupported_platform`.
- [uv](https://docs.astral.sh/uv/). The MCP server is started through it.
- Android platform tools (`adb`) and a phone with USB debugging enabled.
- A TypeSafe API key.

## Install

While developing, or to try it out:

```sh
claude --plugin-dir /path/to/jev-hands
```

Then, in the session:

1. `/jev-hands:setup` - stores the API key. A hidden macOS dialog opens and the
   value goes straight into the keychain (service `jev-hands`, account
   `typesafe-api-key`). It never passes through the conversation, the terminal,
   a command line or a log. `TYPESAFE_API_KEY` in the environment overrides the
   keychain and is meant for development and CI.
2. `/jev-hands:doctor` - checks the platform, uv, the data directory, the API
   key (presence and origin only), uiautomator2, adb, the device and the screen.
   Every row that is not `ok` carries its own fix.

The first run of either command may spend a while building the plugin
environment. If the MCP server started before that finished, reconnect
`jev-hands-android` in `/mcp`.

## What lands on the phone

One file: `/data/local/tmp/u2.jar`, about 3.5 MB, pushed by uiautomator2 when it
first connects. It is run with `app_process` as the shell user.

- **No APK is installed.** This is why the plugin requires `uiautomator2 >= 3.7`:
  older releases fall back to installing an APK.
- **The input method is not changed.** Text is typed through the accessibility
  set-text path, not through the clipboard and not by installing a keyboard.
- **`monkey` is not used to start apps.** Some apps simply never come up under
  it, and uiautomator2's own `app_start()` falls back to it whenever no
  activity is given. So the plugin asks the package manager for the launcher
  component and starts it with `am start -n package/activity`, whether or not
  an activity was passed in. `app_start()` is only reached when the package
  manager resolves nothing at all.

To remove it:

```sh
adb -s <serial> shell rm /data/local/tmp/u2.jar
```

The agent process on the phone runs as the shell user and the system may kill
it. The plugin reconnects automatically, three attempts two seconds apart.

## Known limits

- **Nothing is held back.** There is no list of forbidden buttons and no word
  that lifts the bar. Whatever passes the confidence tier is executed. Which
  taps pay, send, delete or change an account is for the planner's plan to
  know and the user to decide; the phone's own confirmations - payment sheets,
  biometrics - are the last line, not this plugin.
- **Verification challenges and permission prompts are for humans**, and the
  popup probability reads them unevenly: a slider challenge page measured 0.95
  against the 0.5 threshold and would stop the loop, while the transparent
  routing screen one app puts in front of it measured about 0.3 and would not.
  Do not rely on either reading. The planner has to recognise one from the
  candidate table ("slide to verify", "tap the icons in order", a puzzle), tell
  the user what to do on the phone, and carry on from whatever is on the screen
  afterwards. The plugin neither solves a challenge nor accepts a permission
  prompt.
- **Apps that switch their accessibility tree off, and secure windows.** Both
  come back as a window with nothing in it, and both `adb uiautomator dump` and
  uiautomator2 go through the same channel, so neither can read them. The
  plugin reports `observe_failed` with `reason: hidden_tree`. See
  [Secure screens](#secure-screens); there is no text-only way around it.
- **No vision in the loop.** Jev reads text. Screenshots exist so Claude can look
  when the text path fails; they play no part in the decision.
- **Pages that never go idle.** A page with autoplaying video on it may refuse to
  produce a tree. uiautomator2 handles this far better than the adb dump does,
  but it is not guaranteed.
- **Thresholds are tied to one model version.** The three confidence tiers come
  from roughly a hundred calls across two test rounds; `reached_threshold` and
  `check_threshold` were calibrated on `jev-1.13.0` on 2026-09-19, and
  `reached_entry_threshold`, which confirms a stop, and
  `reached_content_threshold`, which opens the gate, on the same model
  on 2026-09-20, from 56 labelled screens off three rounds on a phone. When
  the model version in a response differs from that one, the decision carries
  a `model_drift` flag and the numbers need calibrating again;
  `scripts/calibrate_reached.py` is how.
- **Automation can trip an app's fraud checks.** Use a test account.

## Configuration

Precedence, highest first:

1. the `policy` argument on a tool call
2. `.claude/jev-hands.local.md` in the working directory, as YAML frontmatter
3. `${CLAUDE_PLUGIN_DATA}/config.json`
4. built-in defaults

Recognised settings, with their defaults:

| Setting | Default | What it does |
|---|---|---|
| `act_threshold` | 0.65 | At or above this, the decision is executed. |
| `ask_threshold` | 0.45 | Between this and `act_threshold`, nothing is executed and the step ends as `ask_low_confidence`. |
| `popup_threshold` | 0.5 | At or above this, the step ends as `ask_popup`. |
| `reached_threshold` | 0.25 | At or above this, the `end_state` gate opens and the two confirmation answers from the same request are read. On its own it stops nothing; together with `reached_entry_threshold` it decides the stop. |
| `check_threshold` | 0.75 | `jev_check` reads at or above this as `yes`, at or below its mirror (0.25) as `no`, and anything between as `unsure`. The raw probability comes back either way. It is `jev_check`'s own bar and nothing else's. |
| `reached_content_threshold` | 0.4 | Gate opener only. At or above this, `reached_content` says the content is on the screen and the gate opens, so a low `reached` cannot hide a destination. It is reported on every gate hit and takes no part in the stop. |
| `reached_entry_threshold` | 0.4 | At or below this, `reached_entry` says the screen is not merely a way to the destination. With `reached_threshold` this is what confirms a stop. |
| `reached_confirm_threshold` | - | The older name from when one question decided the stop. Still accepted: it sets `reached_content_threshold` - which now only opens the gate - and prints a note on stderr. |
| `settle_min_seconds` | 0.35 | Waited after every action before the screen is read. |
| `settle_extra_seconds` | 0.45 | Waited a second time, and only when that read found the screen unchanged. |
| `settle_confirm_seconds` | 0 | Waited before reading a second time when the action did change the screen, so that only a screen two reads agree on is handed to the next step. Off by default; see below. |
| `settle_seconds` | - | The older name for one fixed wait. Still accepted: it sets `settle_min_seconds` and `settle_extra_seconds` to the value given. It does not touch `settle_confirm_seconds`. |
| `token_budget` | 1500 | Size of the candidate table. |
| `max_steps` | 5 | Steps per `jev_run`. |
| `model` | `jev-latest` | Which Jev model answers. |
| `calibrated_model` | `jev-1.13.0` | The model the thresholds were set on; anything else raises `model_drift`. |
| `verify_before_act` | true | Read the screen once more immediately before acting. |

`reached_threshold` and `check_threshold` were calibrated on `jev-1.13.0`
(2026-09-19): ten screens that had reached their destination read 0.33 to 0.97
and eight that had not read 0.02 to 0.05, so 0.5 would have missed the weakest
true positive. Recalibrate when `model_drift` shows up.

On a phone, that gap turned out to be narrower than the calibration screens
suggested: a delivery app home page read 0.25 to 0.29 against an end state its
search bar only led to, and stopped three runs out of ten before the search
page was open. `reached_threshold` therefore no longer stops anything by
itself. It opens a gate, and two further questions about the same screen
decide.

### The two-part confirmation

One confirmation question used to do the job, worded to say no for an entry
page and yes for the content itself. Round 7 measured it on 19 real screens
and it could not be made to work: rewording lifted entry pages by about 0.35
and real content pages by about 0.15, so the screens that were the destination
read 0.39 to 0.97 and the screens that were not read 0.37 to 0.64. The two
groups overlapped and no threshold sat between them. The question was being
asked to judge two things at once.

It is now two literal questions, both noul, both riding in the same request as
everything else the step asks:

- **`reached_content`** - is the content described by `end_state` itself
  visible on this screen right now?
- **`reached_entry`** - is this screen a home, entry, menu or navigation page
  that only leads to that state, rather than the state itself?

Neither question is asked whether the run has arrived; the code decides that.
The gate opens when `reached` clears `reached_threshold` **or**
`reached_content` clears `reached_content_threshold`, so a destination that
reads low on `reached` is no longer invisible. The step then stops as
`reached` when `reached` is over its bar **and** `reached_entry` is at or
under `reached_entry_threshold`.

`reached_content` opens the gate and stops nothing. Round 8 measured it on 60
fresh screens with the confirmation still reading it: the two label groups
overlapped from 0.30 to 0.60, it refused three screens that were the
destination - two of them results pages that had not finished drawing - and
the four screens it let through were all caught by `reached_entry` anyway. On
the 56 recorded screens, taking it out of the confirmation changes nothing at
all: 0 screens stopped on wrongly and 2 destinations missed, the same two
counts as before, both of them refused by the `reached` gate at 0.22.

Jev answers the questions of one request in parallel, so the two cost tokens
and no time: about 880 characters of question between them against the single
confirmation's 840, so on the order of ten extra input tokens on each per-step
request that carries an `end_state`. Two questions, not two requests. Asked as
a request of its own, one of them cost a median 496 ms and about 1900 input
tokens, because the whole screen went with it.

Otherwise the step does whatever the model picked, reporting
`reached_candidate: true`, `reached_content`, `reached_entry` and
`reached_confirmed: false` - and when the model picked nothing, the step still
stops as `reached`, marked `reached_confirmed: false`, with a hint saying to
verify. That last case is the one round 6 got wrong: it ended as `stop_none`,
whose hint sends the caller off to re-plan, on two screens that were the
destination. Confirmations that do not come back at all leave the step
stopping on the gate, because being wrong by stopping costs a round trip and
being wrong by acting does not come back.

One more thing changed with them. `ask_contradiction` is still checked before
the gate, but a step that was given an `end_state` now carries
`reached_candidate`, `reached_content` and `reached_entry` whichever way the
answers contradicted each other, its hint quotes all three, and
`contradiction_kind` names the one that fired. Round 7 lost a whole run to
that stop arriving with nothing to look at. Batch G reported the readings for
one kind of contradiction only - `action: none` while `tap_target` still
points hard at an element - and rounds 7 and 8 then hit four contradictions
on a phone, four out of four the other way round: `action: tap_element` with
`tap_target: none`, twice on a screen that was the destination.

### A screen that has not finished drawing

A candidate table of three rows or fewer, one step after a table of eight rows
or more in the same run, is a frame the app has not drawn yet rather than a
screen to read an arrival off. Round 8 stopped twice on a delivery app results page
whose tree held a back button and the query text and nothing else; both
screenshots showed a full list of shops. When such a frame would open the
gate, the step waits `settle_extra_seconds` once, reads the tree again and
decides on what came back, and says so with `rerendered: true`. Once per step,
never in a loop: whatever the second look says is what the step works from,
and a second look that cannot be read leaves the first decision standing.

### Calibrating the two bars

`scripts/calibrate_reached.py` is how the two numbers were set and how to set
them again. `tests/fixtures/reached_calibration.jsonl` holds 56 labelled
screens - the exact state objects real steps sent to Jev, scrubbed of personal
data, each marked `destination` or `not_destination` - drawn from three rounds
on a phone. The script sends each one once with the three `end_state`
questions, sweeps `reached_content` over 0.30 to 0.90 against `reached_entry`
over 0.10 to 0.70, and prints, for every pair, how many screens that were not
the destination it would stop on and how many that were it would miss. It
writes the whole sweep to `tests/fixtures/reached_calibration_result.json`
when given `--save-result`. `tests/test_reached_calibration.py` then replays
those recorded probabilities through the shipped defaults offline, so
regenerating the fixture on a newer model fails the suite if the separation is
gone.

On `jev-1.13.0`, 2026-09-20, 26 destination and 30 not-destination screens:
`reached_entry` read 0.07 to 0.40 on the destinations and 0.47 to 0.94 on the
rest - an empty band where the old single question had none - while
`reached_content` still overlapped (0.38 to 0.97 against 0.04 to 0.50). Hence
0.4 for the bar that confirms and 0.4 for the bar that opens the gate: zero
screens stopped on wrongly, two destinations missed, and both of those were
refused by the `reached` gate at 0.22 rather than by either new bar. The sweep
in the fixture has two axes because it was taken when both bars confirmed; the
axis that decides a stop is `reached_entry` alone, and every bar from 0.35 to
0.45 on it gives those same two counts.

The server is started with `uv run --project`, which leaves the working
directory alone, so `.claude/jev-hands.local.md` is found next to the code you
are working on rather than inside the plugin.

Example `.claude/jev-hands.local.md`:

```markdown
---
act_threshold: 0.7
max_steps: 3
settle_min_seconds: 0.5
---
```

When `CLAUDE_PLUGIN_DATA` is not set - which happens when the server is started
by hand rather than by Claude Code - the data directory falls back to
`~/.jev-hands/`, and the doctor says so.

Credentials are never written to the data directory. Neither are screenshots:
those go to a private directory under `$TMPDIR`, made on first use and removed
when the server exits, because nothing needs them once the step they belong to
has been read.

## Safety

- The model only ever returns an index and probabilities. Coordinates,
  selectors, shell and code are produced by this plugin, never by the model.
- Text on the screen is data. Every request says so.
- The plugin holds nothing back. There is no list of element names it refuses
  and no word that lifts the bar: whatever passes the confidence tier is
  executed. Consequential actions belong in the planner's plan, and the
  phone's own confirmations are the last line.
- Writes are never retried. When the outcome is unknown the plugin says
  `execute_uncertain` and stops.
- The screen is read once more immediately before every action. If it moved in
  between, nothing is executed and the step ends as `stale_screen`. Every
  window counts as the screen except the decorative ones - the system UI, the
  input method, and edge strips with nothing to interact with - so a permission
  dialog drawn by another package is seen and listed rather than tapped
  through.
- The API key is read into the `Authorization` header and nowhere else. It is
  never logged, never returned by a tool, never placed on a command line.

## Building a skill on top

`skills/jev-hands/SKILL.md` is the reference for a planner: what the nine tools
do, how to write `goal` and `end_state`, what the confidence tiers mean, and
what to do about each stop reason. Its last section walks through writing a
task-specific skill - the app knowledge that jev-hands deliberately does not
have.

## Tools

`jev_doctor`, `jev_device`, `jev_launch`, `jev_observe`, `jev_decide`,
`jev_step`, `jev_run`, `jev_act`, `jev_check`. Each returns a JSON object with a
`summary` string.

`jev_check` returns both the raw `probability` and a `tier` of `yes`, `unsure`
or `no`, read off `check_threshold`.

`jev_step` and `jev_run` record their `timings` in milliseconds: `observe_ms`,
`decide_ms`, `confirm_ms`, `verify_ms`, `execute_ms`, `settle_ms`,
`settle_confirm_ms` and `observe_after_ms`, which covers every read taken after
the action. `confirm_ms` appears on a step whose `end_state` gate opened and is
always 0: the confirmation rides in the request the step was making anyway. The
field is kept so that a harness summing the segments keeps working. Alongside
them is `reused_observation`, true on the steps inside a `jev_run` that started
from the screen the previous step had already read.

The screen read after an action is handed to the next step as it is, which is
what makes `reused_observation` worth having. `settle_confirm_seconds` adds a
second read that has to agree with the first before the screen is handed on,
and is off by default: a round of ten device runs with it on cost about 750 ms
on 92% of the executed steps and refused 62% of the reuse, to save the four or
five seconds of `stale_screen` retries that reuse had cost over the same ten
runs. Switch it on to try that trade again; when it is on and the two reads
disagree, the newer one is still reported, the step is marked `unstable: true`,
and the next step reads the tree for itself. Either way the freshness check
immediately before the action is what protects the tap, and a page that moved
in between costs one `stale_screen` step rather than a wrong tap.

`jev_run` also returns `stale_count`, `reused_count`, `gate_hits` (steps where
the `reached` gate opened), `confirmed` (gate hits the confirmation backed up)
and `unconfirmed_stops` (steps that stopped as `reached` although it did not).

## Development

```sh
uv sync
uv run pytest
```

Tests are offline: they run against recorded UI trees in `tests/fixtures/` and a
fake adapter. `scripts/replay_decide.py` replays those fixtures against the real
Jev API to measure accuracy and confidence, and is how thresholds get
recalibrated when the model moves.

## License

MIT
