"""Checks of the Hermes symbols this plugin relies on, and the notices when one is missing."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from . import config

_H: dict[str, Any] = {
    "checks": {}, "noticed": set(),
    "config_errors": set(), "data_errors": set(),
    "pending_notices": [],  # one-off notices waiting to be posted to each session
}


def _set_data_errors(errors: set) -> None:
    _H["data_errors"] = errors
    for reason in errors:
        _notice(reason)


def _notice(reason: str) -> None:
    """ONE notice per process per reason, kept for transform_llm_output to post (once per session).
    `reason` is a plain sentence for the user."""
    if reason in _H["noticed"]:
        return
    _H["noticed"].add(reason)
    _H["pending_notices"].append(reason)


def _healthy(name: str) -> bool:
    """True unless the register-time check `name` failed (unknown => assume OK)."""
    return _H["checks"].get(name, (True, ""))[0]


def _module_file(module: str) -> Path | None:
    """Source file of `module`, found without importing it or its parents."""
    import importlib.util

    parts = module.split(".")
    spec = importlib.util.find_spec(parts[0])  # top-level name only: imports nothing
    if spec is None:
        return None
    if len(parts) == 1:
        return Path(spec.origin) if spec.origin and spec.origin.endswith(".py") else None
    dirs = [Path(d) for d in spec.submodule_search_locations or []]
    for part in parts[1:-1]:
        dirs = [d / part for d in dirs]
    for d in dirs:
        for f in (d / (parts[-1] + ".py"), d / parts[-1] / "__init__.py"):
            if f.is_file():
                return f
    return None


def _binding(body: list[Any], name: str) -> Any:
    """The statement in `body` that defines `name` (def, class, assignment, import), else None."""
    import ast

    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(n, ast.Name) and n.id == name for t in targets for n in ast.walk(t)):
                return node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if any((a.asname or a.name.split(".")[0]) == name for a in node.names):
                return node
        elif isinstance(node, (ast.If, ast.Try)):
            inner = node.body + node.orelse + getattr(node, "finalbody", [])
            inner += [s for h in getattr(node, "handlers", []) for s in h.body]
            found = _binding(inner, name)
            if found is not None:
                return found
    return None


def _has_symbol(module: str, name: str) -> bool:
    """True if `module` really defines `name` ("Class.method" looks inside the class).

    No side effects: a loaded module is asked directly, else its source is parsed, never
    imported. A name left in a comment or a string does not count. Any error is False.
    """
    import ast

    try:
        mod = sys.modules.get(module)
        if mod is not None:
            obj: Any = mod
            for part in name.split("."):
                obj = getattr(obj, part)
            return True
        path = _module_file(module)
        if path is None:
            return False
        body = ast.parse(path.read_text(errors="ignore")).body
        *owners, last = name.split(".")
        for owner in owners:
            node = _binding(body, owner)
            if not isinstance(node, ast.ClassDef):
                return False
            body = node.body
        return _binding(body, last) is not None
    except Exception:
        return False


def _tested_version() -> str:
    try:
        import re

        m = re.search(r'^tested_hermes:\s*"?([0-9][^"\s]*)', (Path(__file__).parent / "plugin.yaml").read_text(), re.M)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _health() -> dict[str, tuple[bool, str]]:
    """Cheap, offline checks of every Hermes surface this plugin leans on."""
    out: dict[str, tuple[bool, str]] = {}

    def check(name: str, fn: Any) -> None:
        try:
            result = fn()
            out[name] = (True, "") if result else (False, "missing or changed")
        except Exception as exc:
            out[name] = (False, f"{type(exc).__name__}: {exc}")

    def has(*symbols: tuple[str, str]) -> bool:
        """True if every symbol exists, else False."""
        return all(_has_symbol(module, symbol) for module, symbol in symbols)

    def listed(module: str, table: str, name: str) -> bool:
        """True if Hermes still lists `name` in `module.table`: it only logs a warning for an unknown name."""
        import importlib

        return name in getattr(importlib.import_module(module), table)

    check("middleware", lambda: listed("hermes_cli.middleware", "VALID_MIDDLEWARE", "llm_execution"))
    check("llm_request", lambda: listed("hermes_cli.middleware", "VALID_MIDDLEWARE", "llm_request"))
    check("transform_llm_output", lambda: listed("hermes_cli.plugins", "VALID_HOOKS", "transform_llm_output"))
    check("pre_llm_call", lambda: listed("hermes_cli.plugins", "VALID_HOOKS", "pre_llm_call"))
    check("usage_api", lambda: has(("agent.usage_pricing", "normalize_usage")))
    check("config_api", lambda: has(("hermes_constants", "get_hermes_home")))
    check("cron_execution", lambda: callable(getattr(config._CTX, "current_cron_execution", None)))
    check("single_query", lambda: has(("tools.approval_context", "_is_single_query_approval_context")))
    check("post_message", lambda: callable(getattr(config._CTX, "post_message", None)))
    check("set_config", lambda: callable(getattr(config._CTX, "set_config", None)))

    # Information only: a newer Hermes is not a fault. The checks above catch real breakage.
    try:
        import importlib.metadata

        cur = importlib.metadata.version("hermes-agent")
    except Exception:
        cur = "?"
    out["hermes_version"] = (True, f"{cur} (tested {_tested_version() or '?'})")
    return out


# One plain sentence per check, printed in the chat. The technical name and detail stay in
# `/cacheguard` (health section) only.
_HEALTH_EFFECTS = {
    "usage_api": "cacheguard cannot read cache numbers: results may be missing.",
    "llm_request": "cacheguard cannot block here: it only warns.",
    "transform_llm_output": "no result lines.",
    "pre_llm_call": "cacheguard cannot block here: it only warns.",
    "cron_execution": "cacheguard may treat some cron runs as chats.",
    "single_query": "with chat -q, cacheguard may block where it should only warn.",
    "post_message": "cacheguard cannot show its lines in this Hermes.",
    "set_config": "cacheguard cannot save settings in this Hermes.",
    "middleware": "cacheguard is off: it could not start.",
}


def _run_health() -> None:
    _H["checks"] = _health()
    for name, effect in _HEALTH_EFFECTS.items():
        if not _healthy(name):
            _notice(effect)


def _health_text() -> str:
    bad = [f"{k}: {d}" + (f" ({_HEALTH_EFFECTS[k]})" if k in _HEALTH_EFFECTS else "")
           for k, (ok, d) in _H["checks"].items() if not ok]
    bad.extend(f"config: {reason}" for reason in sorted(_H["config_errors"]))
    bad.extend(f"data: {reason}" for reason in sorted(_H["data_errors"]))
    if not bad:
        return ""
    return "\n".join(["health: degraded"] + [f"  - {b}" for b in bad])
