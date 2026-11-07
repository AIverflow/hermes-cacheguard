"""The /cacheguard commands and the report they print."""

from __future__ import annotations

import math
import os
import sys
from typing import Any

from . import config, confirm, health, measure, storage

_SET_USAGE = (
    "usage: /cacheguard set <name> <value>\n"
    "  cli           off | warn | block   terminal, editor\n"
    "  desktop       off | warn | block   desktop app, web UI\n"
    "  gateway       off | warn | block   Telegram and other messaging platforms\n"
    "  pause_m       number >= 1       alert after this many minutes of pause\n"
    "  size_k        number (in k)     alert from this prompt size, e.g. 30 = 30k (max 2000)\n"
    "  journal_rows  whole number >= 1  most calls kept in the stats (default 10000)\n"
    "  chat_files    whole number >= 1  most chats that keep a tracking file (default 200)\n"
    "  display_on_success  on | off    show the result line when the cache was kept (default off)\n"
    "  block_images  on | off          also block a message with an image; attach it again to send (default off)\n"
    "  tiny_block    on | off          one short BLOCKED line instead of three (default off)\n"
    "  model <pause_m|size_k|mode> <value>   own value for the model of this chat\n"
    "  model reset                           this chat's model back to the global settings"
)


_SETUP_USAGE = (
    "usage: /cacheguard setup <all|cli|desktop|gateway|model> <off|warn|block> [pause_m] [size_k]\n"
    "  /cacheguard setup all off                   cli, desktop and gateway: off (pause and size unchanged)\n"
    "  /cacheguard setup all block 10 30           cli, desktop and gateway: block\n"
    "  /cacheguard setup gateway warn 10 30        gateway: warn (even if it was off)\n"
    "  /cacheguard setup model block 10 50         the model of this chat only\n"
    "  Give both pause and size, or neither. Pause and size apply to all groups (with model: to that model only)."
)

_HELP_TEXT = (
    "cacheguard help\n"
    "\n"
    "Commands\n"
    "  /cacheguard                          settings, and the table: how often the cache was lost\n"
    "  /cacheguard all                      every recorded model\n"
    "  /cacheguard <model>                  only models whose name contains this text\n"
    "  /cacheguard setup <all|cli|desktop|gateway|model> <mode> [pause_m] [size_k]\n"
    "                                       one group, all three, or the model of this chat. Pause and size:\n"
    "                                       give both or neither. For a group they apply to all groups;\n"
    "                                       for model, to that model only\n"
    "  /cacheguard set <name> <value>\n"
    + "\n".join(_SET_USAGE.splitlines()[1:]) + "\n"
    "  /cacheguard reset                    delete saved history\n"
    "  /cacheguard help                     this text\n"
    "\n"
    "Examples (copy and paste)\n"
    "  /cacheguard setup all off            cli, desktop and gateway: off (pause and size unchanged)\n"
    "  /cacheguard setup cli block 10 20    cli: block after a 10 minute pause, prompts of 20k+\n"
    "  /cacheguard setup gateway warn 10 30 gateway warns, even if it was off\n"
    "  /cacheguard setup model block 10 50  the model of this chat: block after a 10 minute pause, prompts of 50k+; it wins over the groups that are not off\n"
    "  /cacheguard set gateway block        guard Telegram and other messaging platforms\n"
    "  /cacheguard set pause_m 1            change only the pause, here 1 minute\n"
    "  /cacheguard set model mode warn      the model of this chat: never blocked, only warned"
)


def _parse_setting(name: str, raw: str) -> tuple[tuple[str, Any, str] | None, str]:
    """Validate one `name value` pair. Return ((config_key, value, shown), "") or (None, error)."""
    name, raw = name.lower(), raw.lower()
    if name in ("cli", "desktop", "gateway"):
        allowed = ("off", "warn", "block")
        if raw not in allowed:
            return None, f"cacheguard: {name} must be one of {', '.join(allowed)}."
        return (name, raw, raw), ""
    if name in ("display_on_success", "block_images", "tiny_block"):
        if raw not in ("on", "off"):
            return None, f"cacheguard: {name} must be on or off."
        return (name, raw == "on", raw), ""
    if name in ("pause_m", "size_k"):
        try:
            number = float(raw)
        except ValueError:
            number = math.nan
        if not config._valid_number(name, number):
            if name == "pause_m":
                return None, "cacheguard: pause_m must be a number >= 1."
            return None, "cacheguard: size_k is in thousands: 20 = 20k. A number from 0 to 2000."
        saved = int(number) if number.is_integer() else number
        return (name, saved, f"{number:g} min" if name == "pause_m" else f"{number:g}k"), ""
    if name in ("journal_rows", "chat_files"):
        try:
            count = int(raw)
        except ValueError:
            count = 0
        if not config._valid_count(count):
            return None, f"cacheguard: {name} must be a whole number, 1 or more."
        return (name, count, str(count)), ""
    return None, ""


def _save_setting(key: str, value: Any) -> None:
    """Write one setting through the plugin context."""
    if config._CTX is None:
        raise RuntimeError("no plugin context")
    config._CTX.set_config(key, value)


_NO_MODEL = "cacheguard: no model seen in this chat yet; send a message first."


def _chat_model(session_id: str) -> str | None:
    """The `provider/model` of this chat, None while the chat has none."""
    stored = storage._get_session(session_id).get("model") if session_id else None
    return stored if isinstance(stored, str) and "/" in stored else None


def _cmd_set_model(args: list[str], session_id: str) -> str:
    """/cacheguard set model <pause_m|size_k|mode> <value> | reset: the model line of this chat's model."""
    reset = [a.lower() for a in args] == ["reset"]
    if not reset and (len(args) != 2 or args[0].lower() not in ("pause_m", "size_k", "mode")):
        return _SET_USAGE
    if not reset:
        name = args[0].lower()
        if name == "mode":
            if args[1].lower() not in config._MODES:
                return "cacheguard: mode must be one of off, warn, block.\n" + _SET_USAGE
            value, shown = args[1].lower(), args[1].lower()
        else:
            parsed, err = _parse_setting(name, args[1])
            if parsed is None:
                return err + "\n" + _SET_USAGE
            _, value, shown = parsed
    stored = _chat_model(session_id)
    if stored is None:
        return _NO_MODEL
    current = config._cfg()["models"]
    lines = [line for line in current if line["model"] != stored]
    if not reset:
        old = next((line for line in current if line["model"] == stored), {"model": stored})
        lines.append({**old, name: value})
    try:
        _save_setting("models", lines)
    except Exception as exc:
        return f"cacheguard: could not write models ({exc})."
    if reset:
        return f"cacheguard: {stored} uses the global settings (saved to config)."
    return f"cacheguard: {stored} {name} = {shown} (saved to config)."


def _cmd_set(args: list[str], session_id: str = "") -> str:
    if args and args[0].lower() == "model":
        return _cmd_set_model(args[1:], session_id)
    if len(args) != 2:
        return _SET_USAGE
    parsed, err = _parse_setting(args[0], args[1])
    if parsed is None:
        return (err + "\n" if err else "") + _SET_USAGE
    name, (key, value, shown) = args[0].lower(), parsed
    try:
        _save_setting(key, value)
    except Exception as exc:
        return f"cacheguard: could not write {name} ({exc})."
    return f"cacheguard: {name} = {shown} (saved to config)."


def _cmd_setup(args: list[str], session_id: str = "") -> str:
    """/cacheguard setup <all|cli|desktop|gateway|model> <mode> [pause_m] [size_k]. Pause and size: both or neither.

    Nothing is written unless every given value is valid.
    """
    if len(args) < 2 or len(args) > 4:
        return _SETUP_USAGE
    if args[0].lower() not in ("all", "model") + _SETUP_GROUPS:
        return f"cacheguard: Unknown group {args[0]}; use all, cli, desktop, gateway or model.\n" + _SETUP_USAGE
    if len(args) == 3:
        return "cacheguard: Give both pause and size, or omit both.\n" + _SETUP_USAGE
    target, mode_raw = args[0].lower(), args[1]
    if mode_raw.lower() not in ("off", "warn", "block"):
        return "cacheguard: mode must be off, warn or block.\n" + _SETUP_USAGE
    groups = [] if target == "model" else list(_SETUP_GROUPS) if target == "all" else [target]
    items = [(g, _parse_setting(g, mode_raw)[0]) for g in groups]
    with_timing = len(args) == 4
    if with_timing:
        pause_item, err = _parse_setting("pause_m", args[2])
        if pause_item is None:
            return err + "\n" + _SETUP_USAGE
        size_item, err = _parse_setting("size_k", args[3])
        if size_item is None:
            return err + "\n" + _SETUP_USAGE
        items += [("pause_m", pause_item), ("size_k", size_item)]
    if target == "model":
        return _setup_model(session_id, mode_raw.lower(), items)
    for name, (key, value, _shown) in items:
        try:
            _save_setting(key, value)
        except Exception as exc:
            return f"cacheguard: could not write {name} ({exc})."
    modes = " · ".join(f"{g} = {mode_shown}" for g, (_key, _value, mode_shown) in items[:len(groups)])
    if not with_timing:
        return f"cacheguard: {modes} (saved to config)."
    shown = {name: parsed[2] for name, parsed in items}
    return (f"cacheguard: {modes} · pause ≥ {shown['pause_m']} · size ≥ {shown['size_k']} "
            "(pause and size apply to all groups; saved to config).")


def _setup_model(session_id: str, mode: str, items: list[tuple[str, Any]]) -> str:
    """Write mode (and the pause and size items, if any) into the line of this chat's model."""
    stored = _chat_model(session_id)
    if stored is None:
        return _NO_MODEL
    current = config._cfg()["models"]
    old = next((line for line in current if line["model"] == stored), {"model": stored})
    new = {**old, "mode": mode, **{name: parsed[1] for name, parsed in items}}
    try:
        _save_setting("models", [line for line in current if line["model"] != stored] + [new])
    except Exception as exc:
        return f"cacheguard: could not write models ({exc})."
    shown = [f"mode = {mode}"] + [
        f"{'pause' if name == 'pause_m' else 'size'} ≥ {parsed[2]}" for name, parsed in items]
    return f"cacheguard: {stored} · {' · '.join(shown)} (this model only; saved to config)."


_SETUP_GROUPS = ("cli", "desktop", "gateway")


# Surfaces known to render a fenced code block as monospace text.
_MARKDOWN_SURFACES = frozenset({"telegram", "discord", "slack", "matrix", "mattermost", "webui"})


def _markdown_surface(platform: str = "") -> bool:
    """True when the reply goes to a Markdown renderer (fence the table), False for raw text.

    Chat apps and the web UI render Markdown; CLI, TUI and desktop are raw. With no named surface,
    the web UI is told apart by its loaded `api.commands` module and an unset HERMES_INTERACTIVE.
    """
    idents = [os.environ.get("HERMES_PLATFORM", ""), platform]
    idents = [str(ident).strip().lower() for ident in idents if str(ident).strip()]
    if idents:
        return any(ident in _MARKDOWN_SURFACES for ident in idents)
    if os.environ.get("HERMES_INTERACTIVE", "").strip().lower() in ("1", "true", "yes", "on"):
        return False  # classic CLI (and the TUI backend before it binds a session)
    webui = sys.modules.get("api.commands")
    return callable(getattr(webui, "execute_plugin_command", None))


def _unknown_argument(word: str) -> str:
    return f"unknown argument '{word}'. Try /cacheguard help"


def _cmd_show(raw_args: str = "", platform: str = "", session_id: str = "") -> str:
    """The /cacheguard command. platform: the surface Hermes calls it from (cli, telegram, ...)."""
    parts = (raw_args or "").split()
    if parts and parts[0].lower() == "help":
        if len(parts) > 1:
            return _unknown_argument(parts[1])
        # A Markdown renderer folds the spaces and joins the lines; a fence keeps them.
        return f"```\n{_HELP_TEXT}\n```" if _markdown_surface(platform) else _HELP_TEXT
    if parts and parts[0].lower() == "set":
        return _cmd_set(parts[1:], session_id)
    if parts and parts[0].lower() == "setup":
        return _cmd_setup(parts[1:], session_id)
    if parts and parts[0].lower() == "reset":
        return "usage: /cacheguard reset" if len(parts) > 1 else _cmd_reset()
    show_all = bool(parts) and parts[0].lower() == "all"  # every row, not the top ones
    return _summary(parts, show_all, platform)


def _cmd_reset() -> str:
    try:
        import shutil

        with storage._storage_lock():
            config._obs_path().unlink(missing_ok=True)
            shutil.rmtree(config._state_dir() / "sessions", ignore_errors=True)
            storage._obs_cache = None
        return "cacheguard: Measurements and session tracking cleared. Settings kept."
    except Exception as exc:  # pragma: no cover
        return f"cacheguard: could not clear observations ({exc})."


def _summary(parts: list[str], show_all: bool, platform: str) -> str:
    # No subcommand: summary. "all" = every row. A word naming a recorded model = model filter.
    word = parts[0].lower() if parts else ""
    if len(parts) > 1:
        return _unknown_argument(parts[1])
    rows = storage._load_observations()
    if word and not show_all and not any(word in str(row.get("model") or "unknown").lower() for row in rows):
        return f"No recorded model matches {parts[0]}. Use /cacheguard all to list recorded models."
    model_filter = "" if show_all else word

    cfg = config._cfg()
    evaluated = measure.evaluate_observations(rows)

    minutes = f"{cfg['pause_m']:g}"
    kilo = f"{cfg['size_k']:g}"
    models = measure._cold_cells(evaluated, model_filter)
    selected = sorted(
        ((model, cells) for model, cells in models.items() if any(cell["calls"] for cell in cells)),
        key=lambda pair: (-sum(cell["calls"] for cell in pair[1]), pair[0]))
    shown = selected if (show_all or model_filter) else selected[:_TOP_ROWS]
    measured = sum(cell["calls"] for _, cells in selected for cell in cells)
    # Same counts as the table cells below (cell "n" or the "/calls" denominator), for the shown rows.
    in_table = sum(cell["calls"] for _, cells in shown for cell in cells)
    pauses = f"{in_table:,} measured pause{'' if in_table == 1 else 's'}"
    lines = [
        f"cacheguard  ·  cli {cfg['cli']}  ·  desktop {cfg['desktop']}  ·  gateway {cfg['gateway']}"
        f"  ·  pause {confirm._pause_text(config._pause_seconds(cfg['pause_m']))}"
        f"  ·  size {cfg['size_k']:g}k  ·  {pauses}",
        "  ·  ".join(f"{name} {_shown_value(cfg[name])}" for name in _OTHER_SETTINGS),
        *(_model_line(line) for line in cfg["models"]),
        "",
        f"setup: /cacheguard setup all block {minutes} {kilo}  (cli, desktop and gateway: block after a {minutes} minute pause, size over {kilo}k tokens)",
        "set: /cacheguard set pause_m 1  (change only the pause, here 1 minute)",
        "help: /cacheguard help",
        "",
    ]
    if health._health_text():  # nothing to say when all is well
        lines += [health._health_text(), ""]
    lines.append("Cache lost, by pause length")
    if models and 0 < measured < measure._MIN_TABLE_CALLS:
        lines.append(f"({measured} measurements so far, need at least {measure._MIN_TABLE_CALLS})")
        return "\n".join(lines)
    table = _pause_table(shown, evaluated, model_filter)
    # A Markdown renderer folds the spaces and loses the columns; a fence keeps them.
    lines += ["```", *table, "```"] if _markdown_surface(platform) else table
    if not models:
        lines.append(f"(no observations for '{model_filter}')" if model_filter
                     else "(no observations yet)")
    elif not selected:
        lines.append("(nothing counted yet: no call after a pause could be compared with the one before it)")
    if len(shown) < len(selected):
        lines.append(f"... {len(selected) - len(shown)} more models: /cacheguard all")
    return "\n".join(lines)


_OTHER_SETTINGS = ("tiny_block", "block_images", "display_on_success", "journal_rows", "chat_files")


def _model_line(line: dict[str, Any]) -> str:
    """One model line for the summary: only the fields it sets."""
    shown = {"pause_m": "pause {:g} min", "size_k": "size {:g}k", "mode": "mode {}"}
    return f"{line['model']}: " + ", ".join(text.format(line[key]) for key, text in shown.items() if key in line)


def _shown_value(value: Any) -> str:
    return ("on" if value else "off") if isinstance(value, bool) else str(value)


def _pause_table(shown: list[tuple[str, list[dict[str, int]]]], evaluated: list[dict[str, Any]],
                 model_filter: str) -> list[str]:
    """The table lines: one row per shown model, one column per pause bucket, then the suggested pause."""
    try:
        suggested = measure._suggested_pauses(evaluated, model_filter)
    except Exception:
        suggested = {}
    labels = [*measure._GAP_LABELS, "suggested"]
    rendered_rows = []
    for model, cells in shown:
        values = []
        for cell in cells:
            if not cell["calls"]:
                values.append("—")
                continue
            values.append(f"{cell['cold']}/{cell['calls']}")
        values.append(suggested.get(model, "—"))
        rendered_rows.append((model, values))
    model_width = max([len("model")] + [len(model) for model, _ in rendered_rows])
    widths = [max([len(label)] + [len(values[i]) for _, values in rendered_rows])
              for i, label in enumerate(labels)]
    table = [f"{'model':<{model_width}}  " + "  ".join(
        label.rjust(width) for label, width in zip(labels, widths, strict=True))]
    for model, values in rendered_rows:
        table.append(f"{model:<{model_width}}  " + "  ".join(
            value.rjust(width) for value, width in zip(values, widths, strict=False)))
    return table


def _cmd_entry(raw_args: str = "", *, platform: str | None = "", session_id: str | None = "") -> str:
    """/cacheguard output with blank lines as one space: the Hermes TUI pager drops
    empty lines (NousResearch/hermes-agent#30782); the CLI shows both the same.
    Hermes passes `platform` and `session_id` to handlers that name them."""
    out = _cmd_show(raw_args, platform=str(platform or "").strip().lower(), session_id=str(session_id or ""))
    return "\n".join(line or " " for line in out.split("\n"))

_TOP_ROWS = 10
