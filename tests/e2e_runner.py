"""Launcher run by the installed Hermes Python (read-only use of the Hermes tree).

    python -I e2e_runner.py cli <hermes args...>      # the hermes CLI
    python -I e2e_runner.py web <path to server.py>   # hermes-webui

Same bootstrap as the gateway contract test: the installed dependency environment is added to
sys.path instead of letting Hermes build one under the temporary HERMES_HOME (that takes ~1 min).
"""
import json
import os
import runpy
import site
import sys
from pathlib import Path

mode, rest = sys.argv[1], sys.argv[2:]
agent = Path(os.environ["HERMES_AGENT_DIR"])
sys.dont_write_bytecode = True
sys.path.insert(0, str(agent))
from pm.environments import install_key, site_packages  # noqa: E402

facts = json.loads((agent.parent / "installs" / install_key(agent) / "facts.json").read_text())
site.addsitedir(str(site_packages(Path(facts["packages"]["venv"]["environment"]))))
sys.argv = ["hermes", "pm", "repair"]  # Hermes' stdlib entrypoint: skips launch prep and activation
import hermes_bootstrap  # noqa: E402,F401

if mode == "cli":
    sys.argv = ["hermes"] + rest
    runpy.run_module("hermes_cli.main", run_name="__main__", alter_sys=True)
else:
    sys.argv = [rest[0]]
    runpy.run_path(rest[0], run_name="__main__")
