#!/usr/bin/env python3
"""Check the plugin against the Hermes that Python can import right now.

Run it with Hermes' own Python. Prints one `FAIL name: reason` line per failed `_health()`
check and exits 1 if any failed. Standard library only.

    python scripts/hermes_compat.py [path/to/__init__.py]
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def failed_checks(plugin_file: Path) -> dict:
    """{check name: reason} for every `_health()` check that failed."""
    spec = importlib.util.spec_from_file_location("cacheguard_compat", plugin_file)
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    try:
        from hermes_cli.plugins import PluginContext
        plugin._CTX = PluginContext  # no live context here: the class carries the same post_message
    except Exception:
        pass  # Hermes not importable: its checks fail below, by name
    return {name: why for name, (ok, why) in plugin._health().items() if not ok}


def main(argv: list) -> int:
    plugin_file = Path(argv[1]) if len(argv) > 1 else ROOT / "__init__.py"
    try:
        failed = failed_checks(plugin_file)
    except Exception as exc:
        failed = {"plugin load": f"{type(exc).__name__}: {exc}"}
    for name, why in failed.items():
        print(f"FAIL {name}: {why}")
    if not failed:
        print("all health checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
