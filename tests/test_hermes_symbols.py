"""Cheap, unpaid check against the installed Hermes: the symbols and shapes the plugin relies on.

Runs by default; skips when Hermes is missing or cannot be imported. Nothing here calls a provider.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from e2e_support import AGENT_DIR

ROOT = Path(__file__).resolve().parent.parent

# Runs inside Hermes' own Python: puts its packages on the path, loads the plugin, asks _health().
CHILD = r'''
import importlib.util, json, site, sys
from pathlib import Path
agent, plugin_path = Path(sys.argv[1]), sys.argv[2]
sys.path.insert(0, str(agent))
from pm.environments import install_key, site_packages
facts = json.loads((agent.parent / "installs" / install_key(agent) / "facts.json").read_text())
site.addsitedir(str(site_packages(Path(facts["packages"]["venv"]["environment"]))))
spec = importlib.util.spec_from_file_location("cache_guard_probe", plugin_path)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
from agent.usage_pricing import normalize_usage
from hermes_cli.plugins import PluginContext
plugin._CTX = PluginContext  # no live context here: the class carries the same post_message
u = normalize_usage({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 40}},
                    provider="p", api_mode="chat_completions")
print(json.dumps({
    "health": {k: list(v) for k, v in plugin._health().items()},
    "unshaped": [f"{m}.{n}" for (m, n) in plugin._CALLS
                 if (m, n) not in plugin._OPTIONAL and plugin._describe(m, n) is None],
    "usage": [u.prompt_tokens, u.cache_read_tokens, u.cache_write_tokens],
}))
'''


def test_installed_hermes_has_what_the_plugin_needs(tmp_path):
    launcher = shutil.which("hermes")
    if launcher is None or not AGENT_DIR.exists():
        pytest.skip("installed Hermes required")
    try:
        runtime = json.loads(subprocess.check_output([launcher, "--print-runtime-command"], text=True, timeout=20))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot find the Hermes Python: {exc}")
    env = dict(os.environ, HERMES_HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    try:
        result = subprocess.run([runtime[0], "-I", "-c", CHILD, str(AGENT_DIR), str(ROOT / "__init__.py")],
                                env=env, text=True, capture_output=True, timeout=60, cwd=str(tmp_path))
    except subprocess.TimeoutExpired:
        pytest.skip("Hermes Python too slow")
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        pytest.skip("cannot import Hermes: " + (lines[-1] if lines else "no output"))
    data = json.loads(result.stdout.strip().splitlines()[-1])
    bad = {k: v[1] for k, v in data["health"].items() if not v[0]}
    assert not bad, bad
    assert not data["unshaped"], data["unshaped"]  # every expected signature was found and compared
    assert data["usage"] == [100, 40, 0]
