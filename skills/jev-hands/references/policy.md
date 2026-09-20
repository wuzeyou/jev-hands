# Tuning through `policy`

This section from `SKILL.md`, word for word. `README.md` has the same keys
with their defaults and what each one does.

Every tool takes an optional `policy` object that overrides the configuration
for that call: `act_threshold`, `ask_threshold`, `popup_threshold`,
`reached_threshold`, `reached_content_threshold`, `reached_entry_threshold`,
`check_threshold`, `settle_min_seconds`,
`settle_extra_seconds` (`settle_seconds` still works and sets both), `token_budget`,
`max_steps`, `model`, `verify_before_act`.

Project-wide defaults go in `.claude/jev-hands.local.md` as YAML frontmatter.
Precedence is: tool argument, then that file, then
`${CLAUDE_PLUGIN_DATA}/config.json`, then built-in defaults.
