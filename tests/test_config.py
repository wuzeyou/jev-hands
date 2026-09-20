"""Configuration precedence and the small frontmatter reader."""

from __future__ import annotations

import json

import pytest

from jev_hands import config


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(data))
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    return project, data


def write_local(project, body: str) -> None:
    (project / ".claude" / "jev-hands.local.md").write_text(body, encoding="utf-8")


def write_data(data, payload: dict) -> None:
    (data / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def test_the_project_file_is_looked_for_in_the_working_directory(workspace):
    """The MCP server is started with `uv run --project`, which keeps the cwd,
    so `.claude/jev-hands.local.md` next to the user's code is found."""
    project, _ = workspace
    write_local(project, "---\nmax_steps: 2\n---\n")
    assert config.load_policy(cwd=project).max_steps == 2
    sources = config.config_sources(cwd=project)
    assert sources["local_config_present"] is True
    assert sources["local_config_path"].endswith(".claude/jev-hands.local.md")


def test_defaults_when_nothing_is_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "empty"))
    policy = config.load_policy(cwd=tmp_path / "nowhere")
    assert policy.thresholds.act == 0.65
    assert policy.thresholds.ask == 0.45
    assert policy.thresholds.popup == 0.5
    assert policy.thresholds.reached == 0.25
    assert policy.thresholds.check == 0.75
    assert policy.thresholds.reached_content == 0.4
    assert policy.thresholds.reached_entry == 0.4
    assert policy.token_budget == 1500
    assert policy.max_steps == 5
    assert policy.settle_min_seconds == 0.35
    assert policy.settle_extra_seconds == 0.45
    assert policy.model == "jev-latest"


def test_the_data_directory_file_beats_the_defaults(workspace):
    project, data = workspace
    write_data(data, {"act_threshold": 0.7, "max_steps": 9})
    policy = config.load_policy(cwd=project)
    assert policy.thresholds.act == 0.7
    assert policy.max_steps == 9


def test_the_project_file_beats_the_data_directory(workspace):
    project, data = workspace
    write_data(data, {"act_threshold": 0.7, "max_steps": 9})
    write_local(project, "---\nact_threshold: 0.8\n---\n\nnotes\n")
    policy = config.load_policy(cwd=project)
    assert policy.thresholds.act == 0.8
    assert policy.max_steps == 9


def test_the_tool_argument_beats_everything(workspace):
    project, data = workspace
    write_data(data, {"act_threshold": 0.7})
    write_local(project, "---\nact_threshold: 0.8\n---\n")
    policy = config.load_policy({"act_threshold": 0.95}, cwd=project)
    assert policy.thresholds.act == 0.95


def test_the_check_threshold_comes_from_config(workspace):
    project, _ = workspace
    write_local(project, "---\ncheck_threshold: 0.6\nreached_threshold: 0.4\n---\n")
    policy = config.load_policy(cwd=project)
    assert policy.thresholds.check == 0.6
    assert policy.thresholds.reached == 0.4
    assert policy.thresholds.reached_content == 0.4, "its own key, not `check`"
    assert policy.thresholds.reached_entry == 0.4


def test_the_two_confirmation_thresholds_come_from_config(workspace):
    project, _ = workspace
    write_local(
        project,
        "---\nreached_content_threshold: 0.35\nreached_entry_threshold: 0.6\n---\n",
    )
    policy = config.load_policy(cwd=project)
    assert policy.thresholds.reached_content == 0.35
    assert policy.thresholds.reached_entry == 0.6
    assert policy.thresholds.check == 0.75


def test_the_old_confirmation_key_still_sets_the_content_bar(workspace, capsys):
    """Batch G split the one confirmation in two. The old key names neither,
    so it keeps setting the one it used to be and says so on stderr."""
    project, _ = workspace
    write_local(project, "---\nreached_confirm_threshold: 0.35\n---\n")
    policy = config.load_policy(cwd=project)
    assert policy.thresholds.reached_content == 0.35
    assert policy.thresholds.reached_entry == 0.4, "untouched by the old key"
    note = capsys.readouterr().err
    assert "reached_confirm_threshold" in note
    assert "reached_content_threshold" in note


def test_the_new_content_key_wins_over_the_old_one(workspace):
    project, _ = workspace
    write_local(
        project,
        "---\nreached_confirm_threshold: 0.35\nreached_content_threshold: 0.6\n---\n",
    )
    policy = config.load_policy(cwd=project)
    assert policy.thresholds.reached_content == 0.6


def test_the_older_settle_seconds_key_sets_both_halves(workspace):
    project, _ = workspace
    write_local(project, "---\nsettle_seconds: 1.0\n---\n")
    policy = config.load_policy(cwd=project)
    assert policy.settle_min_seconds == 1.0
    assert policy.settle_extra_seconds == 1.0


def test_naming_a_settle_half_beats_the_older_key(workspace):
    project, _ = workspace
    write_local(project, "---\nsettle_seconds: 1.0\nsettle_extra_seconds: 0.0\n---\n")
    policy = config.load_policy(cwd=project)
    assert policy.settle_min_seconds == 1.0
    assert policy.settle_extra_seconds == 0.0


def test_the_stability_wait_is_configurable_on_its_own(workspace):
    project, _ = workspace
    write_local(project, "---\nsettle_seconds: 1.0\nsettle_confirm_seconds: 0.0\n---\n")
    policy = config.load_policy(cwd=project)
    assert policy.settle_min_seconds == 1.0
    assert policy.settle_confirm_seconds == 0.0
    assert config.load_policy(cwd=project / "nowhere").settle_confirm_seconds == 0.0

    write_local(project, "---\nsettle_confirm_seconds: 0.25\n---\n")
    assert config.load_policy(cwd=project).settle_confirm_seconds == 0.25


def test_the_removed_word_list_keys_are_no_longer_recognised(workspace):
    """Nothing holds an action back any more, so a leftover word list in
    somebody's config must be ignored rather than quietly half-honoured."""
    project, _ = workspace
    leftovers = "extra_deny" + "list: [wire transfer]\n" + "risk_" + "words: [approve]"
    write_local(project, f"---\n{leftovers}\n---\n")
    assert config.resolve_settings(cwd=project) == {}


def test_verify_before_act_can_be_turned_off(workspace):
    project, _ = workspace
    assert config.load_policy(cwd=project).verify_before_act is True
    write_local(project, "---\nverify_before_act: false\n---\n")
    assert config.load_policy(cwd=project).verify_before_act is False


def test_frontmatter_handles_scalars_inline_lists_and_block_lists():
    parsed = config.parse_frontmatter(
        "---\n"
        "act_threshold: 0.7\n"
        "model: jev-latest\n"
        "enabled: true\n"
        "nothing: null\n"
        "inline: [a, b]\n"
        "block:\n"
        "  - one\n"
        "  - two\n"
        "---\n"
        "body text\n"
    )
    assert parsed["act_threshold"] == 0.7
    assert parsed["model"] == "jev-latest"
    assert parsed["enabled"] is True
    assert parsed["nothing"] is None
    assert parsed["inline"] == ["a", "b"]
    assert parsed["block"] == ["one", "two"]


def test_a_file_without_frontmatter_is_ignored():
    assert config.parse_frontmatter("just some markdown\n") == {}


def test_unknown_keys_are_dropped(workspace):
    project, _ = workspace
    write_local(project, "---\nact_threshold: 0.8\nsomething_else: 12\n---\n")
    settings = config.resolve_settings(cwd=project)
    assert settings == {"act_threshold": 0.8}


def test_the_data_directory_falls_back_when_the_variable_is_unset(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    directory, is_fallback = config.data_dir()
    assert is_fallback is True
    assert directory == config.FALLBACK_DATA_DIR

    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", "/somewhere/else")
    directory, is_fallback = config.data_dir()
    assert is_fallback is False
    assert str(directory) == "/somewhere/else"


def test_config_sources_reports_what_exists_without_reading_secrets(workspace):
    project, data = workspace
    write_data(data, {"act_threshold": 0.7})
    sources = config.config_sources(cwd=project)
    assert sources["data_config_present"] is True
    assert sources["local_config_present"] is False
    assert sources["data_dir_is_fallback"] is False


def test_a_malformed_data_config_is_ignored_rather_than_fatal(workspace):
    project, data = workspace
    (data / "config.json").write_text("{not json", encoding="utf-8")
    policy = config.load_policy(cwd=project)
    assert policy.thresholds.act == 0.65
