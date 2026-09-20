"""Configuration, in precedence order.

1. the ``policy`` argument passed to a tool call
2. ``.claude/jev-hands.local.md`` in the working directory (YAML frontmatter)
3. ``${CLAUDE_PLUGIN_DATA}/config.json``
4. built-in defaults

Only non-sensitive settings live here. The API key never does.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .core.policy import Policy, Thresholds

LOG = logging.getLogger(__name__)

LOCAL_CONFIG_RELPATH = Path(".claude") / "jev-hands.local.md"
DATA_CONFIG_NAME = "config.json"
FALLBACK_DATA_DIR = Path.home() / ".jev-hands"

KNOWN_KEYS = (
    "act_threshold",
    "ask_threshold",
    "popup_threshold",
    "reached_threshold",
    "check_threshold",
    "reached_content_threshold",
    "reached_entry_threshold",
    # Legacy name for `reached_content_threshold`, from when one confirmation
    # question decided the stop on its own. Kept working, with a note - and
    # the bar it sets no longer decides a stop at all; it opens the gate.
    "reached_confirm_threshold",
    "verify_before_act",
    "settle_min_seconds",
    "settle_extra_seconds",
    "settle_confirm_seconds",
    # Legacy name for the two settle keys above, kept working on purpose.
    "settle_seconds",
    "token_budget",
    "max_steps",
    "model",
    "calibrated_model",
)


def data_dir() -> Tuple[Path, bool]:
    """Return the persistent data directory and whether it is the fallback.

    Claude Code sets ``CLAUDE_PLUGIN_DATA`` for plugin processes. When the server
    is started by hand there is nothing to inherit, so fall back to
    ``~/.jev-hands`` and let the doctor say so.
    """
    raw = os.environ.get("CLAUDE_PLUGIN_DATA")
    if raw:
        return Path(raw), False
    return FALLBACK_DATA_DIR, True


def plugin_root() -> Optional[Path]:
    raw = os.environ.get("CLAUDE_PLUGIN_ROOT")
    return Path(raw) if raw else None


# --- a very small YAML frontmatter reader ------------------------------------
# Only what a settings block needs: scalars, inline lists and block lists. A
# real YAML parser is not worth a third-party dependency in an MCP server.


def _coerce(raw: str) -> Any:
    text = raw.strip()
    if not text:
        return ""
    if text[0] in "\"'" and text[-1] == text[0] and len(text) >= 2:
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "none", "~"):
        return None
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_coerce(part) for part in inner.split(",")]
    try:
        if "." in text or "e" in lowered:
            return float(text)
        return int(text)
    except ValueError:
        return text


def parse_frontmatter(text: str) -> Dict[str, Any]:
    """Read the YAML frontmatter block at the top of a markdown file."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    body: List[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        body.append(line)

    out: Dict[str, Any] = {}
    current_list_key: Optional[str] = None
    for line in body:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("- ") and current_list_key:
            out.setdefault(current_list_key, [])
            if isinstance(out[current_list_key], list):
                out[current_list_key].append(_coerce(stripped[2:]))
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if not key:
            continue
        if not value.strip():
            current_list_key = key
            out[key] = []
            continue
        current_list_key = None
        out[key] = _coerce(value)
    return out


def _read_local_config(cwd: Optional[Path] = None) -> Dict[str, Any]:
    base = cwd or Path.cwd()
    path = base / LOCAL_CONFIG_RELPATH
    if not path.is_file():
        return {}
    try:
        return parse_frontmatter(path.read_text(encoding="utf-8"))
    except OSError as exc:
        LOG.warning("could not read %s: %s", LOCAL_CONFIG_RELPATH, type(exc).__name__)
        return {}


def _read_data_config() -> Dict[str, Any]:
    directory, _ = data_dir()
    path = directory / DATA_CONFIG_NAME
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOG.warning("could not read %s: %s", DATA_CONFIG_NAME, type(exc).__name__)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _merge(base: Dict[str, Any], higher: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in higher.items():
        if key in KNOWN_KEYS and value is not None:
            out[key] = value
    return out


def resolve_settings(
    override: Optional[Dict[str, Any]] = None,
    *,
    cwd: Optional[Path] = None,
) -> Dict[str, Any]:
    """Merge the four layers into one flat settings dict."""
    settings: Dict[str, Any] = {}
    settings = _merge(settings, _read_data_config())
    settings = _merge(settings, _read_local_config(cwd))
    settings = _merge(settings, override or {})
    return settings


def _content_default(settings: Dict[str, Any], defaults: Policy) -> float:
    """`reached_confirm_threshold` under its old name still sets the content bar.

    Batch G split the one confirmation question into two literal ones, and the
    old key names neither of them. It sets the content bar, which is the one it
    used to be, and says so on stderr once per load. Since batch H that bar
    only opens the gate; what decides a stop is `reached_entry_threshold`.
    """
    legacy = settings.get("reached_confirm_threshold")
    if legacy is None:
        return defaults.thresholds.reached_content
    print(
        "jev-hands: `reached_confirm_threshold` is the old name for "
        "`reached_content_threshold` and now sets that one, which opens the "
        "gate rather than deciding a stop. The bar that decides a stop is "
        "`reached_entry_threshold`; see the README.",
        file=sys.stderr,
    )
    return float(legacy)


def load_policy(
    override: Optional[Dict[str, Any]] = None,
    *,
    cwd: Optional[Path] = None,
) -> Policy:
    """Build a Policy out of the merged settings."""
    settings = resolve_settings(override, cwd=cwd)
    defaults = Policy()
    thresholds = Thresholds(
        act=float(settings.get("act_threshold", defaults.thresholds.act)),
        ask=float(settings.get("ask_threshold", defaults.thresholds.ask)),
        popup=float(settings.get("popup_threshold", defaults.thresholds.popup)),
        reached=float(settings.get("reached_threshold", defaults.thresholds.reached)),
        check=float(settings.get("check_threshold", defaults.thresholds.check)),
        reached_content=float(
            settings.get("reached_content_threshold", _content_default(settings, defaults))
        ),
        reached_entry=float(
            settings.get("reached_entry_threshold", defaults.thresholds.reached_entry)
        ),
    )
    # `settle_seconds` is the older name from when settling was one fixed wait.
    # It sets both halves; either half named on its own still wins over it.
    legacy_settle = settings.get("settle_seconds")
    settle_min = defaults.settle_min_seconds
    settle_extra = defaults.settle_extra_seconds
    if legacy_settle is not None:
        settle_min = settle_extra = float(legacy_settle)
    return Policy(
        thresholds=thresholds,
        settle_min_seconds=float(settings.get("settle_min_seconds", settle_min)),
        settle_extra_seconds=float(settings.get("settle_extra_seconds", settle_extra)),
        settle_confirm_seconds=float(
            settings.get("settle_confirm_seconds", defaults.settle_confirm_seconds)
        ),
        token_budget=int(settings.get("token_budget", defaults.token_budget)),
        max_steps=int(settings.get("max_steps", defaults.max_steps)),
        model=str(settings.get("model", defaults.model)),
        calibrated_model=str(settings.get("calibrated_model", defaults.calibrated_model)),
        verify_before_act=bool(settings.get("verify_before_act", defaults.verify_before_act)),
    )


def config_sources(cwd: Optional[Path] = None) -> Dict[str, Any]:
    """What the doctor reports: which layers exist, never their secrets."""
    directory, is_fallback = data_dir()
    base = cwd or Path.cwd()
    return {
        "data_dir": str(directory),
        "data_dir_is_fallback": is_fallback,
        "data_config_present": (directory / DATA_CONFIG_NAME).is_file(),
        "local_config_path": str(base / LOCAL_CONFIG_RELPATH),
        "local_config_present": (base / LOCAL_CONFIG_RELPATH).is_file(),
    }
