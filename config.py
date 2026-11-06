"""Settings, paths and the platform table: what cacheguard reads from its config and from the call."""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from . import health

DEFAULTS: dict[str, Any] = {
    "cli": "warn",  # terminal, editor
    "desktop": "warn",  # desktop app, web UI
    "gateway": "off",  # Telegram and other messaging chats
    "display_on_success": False,  # show the result line when the cache was kept (loss and unknown always show)
    "block_images": False,  # also block a message with an image (it must then be attached again)
    "tiny_block": False,  # one short BLOCKED line instead of three
    "size_k": 20,  # thousands of tokens
    "pause_m": 10,
    "journal_rows": 10000,  # most calls kept in the journal (a count, not days)
    "chat_files": 200,  # most chat tracking files kept, newest first
}


_PREFIX = "[CACHEGUARD]"  # the ONE prefix of every line cacheguard adds to a chat
_BLOCKED_PREFIX = _PREFIX + " BLOCKED · "  # starts every "message not sent" reply; history cleaning finds them by it
_POST_PREFIX = "CGUARD"  # prefix of the short posted notices (the TUI status bar cuts them at ~45 characters)


_MIN_PAUSE_S = 60  # shorter pauses (tool calls, quick follow-ups) say nothing about the cache: not kept in the journal
_CAP = 64  # most entries any per-session or per-chat dict of this plugin keeps


def _put_capped(table: dict[Any, Any], key: Any, value: Any) -> Any:
    """table[key] = value; a new key at the cap first drops the oldest. Returns the dropped key, if any."""
    dropped = None
    if key not in table and len(table) >= _CAP:
        dropped = next(iter(table))
        del table[dropped]
    table[key] = value
    return dropped


# The one platform table. Unknown platforms count as messaging (gateway).
_CLI_PLATFORMS = frozenset({"cli", "tui", "local", "acp"})
_DESKTOP_PLATFORMS = frozenset({"desktop", "webui"})
_UNATTENDED_PLATFORMS = frozenset({"cron", "kanban", "subagent", "curator", "api_server", "webhook", "msgraph_webhook"})


def _platform(payload: dict[str, Any]) -> str:
    return str(getattr(payload.get("platform"), "value", payload.get("platform")) or "cli").strip().lower()


def _in_cron_run() -> bool:
    try:
        return _CTX.current_cron_execution() is not None
    except Exception:
        return False


def _is_unattended(payload: dict[str, Any]) -> bool:
    """Nobody is there to answer or read: cacheguard stays out of the way.

    Kanban workers can carry `cli`; Hermes marks them with an env var. Cron runs carry the
    `cron` platform, and Hermes also names the running cron job through its plugin context.
    """
    if _platform(payload) in _UNATTENDED_PLATFORMS or os.environ.get("HERMES_KANBAN_TASK", "").strip():
        return True
    return _in_cron_run()


def _is_cli_or_desktop(payload: dict[str, Any]) -> bool:
    """True for the terminal and the desktop app (they have a screen), false for chat platforms."""
    return _platform(payload) in _CLI_PLATFORMS | _DESKTOP_PLATFORMS


def _surface_mode(payload: dict[str, Any], cfg: dict[str, Any]) -> Any:
    if _platform(payload) in _DESKTOP_PLATFORMS:
        return cfg["desktop"]
    return cfg["cli"] if _is_cli_or_desktop(payload) else cfg["gateway"]


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _valid_number(name: str, value: Any) -> bool:
    """The one rule for pause_m and size_k, in config.yaml and in `/cacheguard set`."""
    if not _finite_number(value):
        return False
    if name == "pause_m":
        return value >= 1 and math.isfinite(float(value) * 60)
    return 0 <= value <= 2000  # size_k: thousands of tokens


def _pause_parts(gap_s: float) -> tuple[int, str]:
    """A pause as (count, unit): seconds up to 1 min, minutes up to 3 h, hours up to 2 d, then days."""
    seconds = max(0, int(gap_s))
    if seconds < 60:
        return seconds, "second"
    if seconds < 180 * 60:
        return seconds // 60, "minute"
    if seconds < 48 * 3600:
        return seconds // 3600, "hour"
    return seconds // 86400, "day"


def _valid_count(value: Any) -> bool:
    """The one rule for journal_rows and chat_files: a whole number, 1 or more."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _pause_seconds(pause_m: Any) -> float:
    return float(pause_m) * 60


def _size_tokens(size_k: Any) -> int:
    return round(float(size_k) * 1000)


def _cfg() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    errors = set()
    try:
        # plugins.entries.cacheguard.settings, written by `/cacheguard set`; Hermes reads it per call
        for key, default in DEFAULTS.items():
            cfg[key] = _CTX.get_config(key, default)
    except Exception:
        cfg = dict(DEFAULTS)
        errors.add("configuration unavailable; using defaults")
        _set_config_errors(errors)
        return cfg
    for key in ("cli", "desktop", "gateway"):
        if cfg[key] is False:  # YAML 1.1 reads an unquoted `off` as False
            cfg[key] = "off"

    def invalid(key: str) -> None:
        cfg[key] = DEFAULTS[key]
        errors.add(f"invalid {key}; using default")

    for key in ("display_on_success", "block_images", "tiny_block"):
        if not isinstance(cfg[key], bool):
            invalid(key)
    for key in ("cli", "desktop", "gateway"):
        if not isinstance(cfg[key], str) or cfg[key] not in ("off", "warn", "block"):
            invalid(key)
    for key in ("pause_m", "size_k"):
        if not _valid_number(key, cfg[key]):
            invalid(key)
    for key in ("journal_rows", "chat_files"):
        if not _valid_count(cfg[key]):
            invalid(key)
    _set_config_errors(errors)
    return cfg


def _set_config_errors(errors: set) -> None:
    # _H is initialized before config is read by registered plugin callbacks.
    health._H["config_errors"] = errors
    for reason in errors:
        health._notice(reason)


def _state_dir() -> Path:
    from hermes_constants import get_hermes_home

    d = Path(get_hermes_home()) / "cacheguard"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _obs_path() -> Path:
    return _state_dir() / "observations.jsonl"


def _bucket_index(gap_s: float, edges: list[float]) -> int:
    for i, edge in enumerate(edges):
        if gap_s < edge:
            return i
    return len(edges)


def _model_key(provider: str | None, model: str | None) -> str:
    model = (model or "").strip() or "unknown"
    provider = (provider or "").strip()
    return f"{provider or 'unknown'}/{model}"


_CTX: Any = None  # plugin context, kept for /cacheguard set (ctx.set_config)
