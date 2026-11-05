"""Fakes for the Hermes modules the plugin imports lazily; no Hermes install needed."""
import importlib.util
import os
import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The real Hermes homes, fixed before any test changes HERMES_HOME. No test may write there.
REAL_HOMES = sorted({Path("~/.hermes").expanduser().resolve(),
                     *([Path(os.environ["HERMES_HOME"]).expanduser().resolve()] if os.environ.get("HERMES_HOME") else [])})


def _home_state(home):
    """What a test could change in a real home: config.yaml and the cacheguard/ folder (name, size, mtime)."""
    state = {}
    for path in [home / "config.yaml", *sorted((home / "cacheguard").rglob("*"))]:
        try:
            info = path.stat()
        except OSError:
            continue
        state[str(path.relative_to(home))] = (info.st_size, info.st_mtime_ns)
    return state


@pytest.fixture(autouse=True)
def real_hermes_home_untouched():
    before = {home: _home_state(home) for home in REAL_HOMES}
    yield
    for home, was in before.items():
        now = _home_state(home)
        changed = sorted(name for name in was.keys() | now.keys() if was.get(name) != now.get(name))
        assert not changed, f"test touched the real Hermes home {home}: {changed}"


class Env:
    """Handle on the fake Hermes environment handed to tests."""

    def __init__(self, home, plugin):
        self.home = home
        self.plugin = plugin
        self.settings = {}        # plugins.entries.cacheguard.settings
        # What the user answers in the box: a callable(command, description, **flags) giving once/session/always/deny/timeout
        # (or a ready gateway decision dict), or None (nobody to ask). It stands behind the fake gateway box below.
        self.approval = None
        self.posted = []          # texts given to ctx.post_message
        self.post_ok = True       # False: post_message returns ok=False; an Exception: it raises

    @property
    def config(self):  # same mapping as `settings`; older tests use this name
        return self.settings

    @config.setter
    def config(self, value):
        self.settings = value

    @property
    def notices(self):  # one-off notices waiting to be posted (reasons, without the prefix)
        return list(self.plugin._H["pending_notices"])

    def warned(self, sid="s1"):
        """The warn-mode marker (or the queued result line carrying it), else None."""
        if sid in self.plugin._WARNED:
            return self.plugin._WARNED[sid]
        note = self.plugin._PENDING.get(sid)
        return note if note and (note.startswith("CGUARD WARN ") or note.startswith("CGUARD OK ")) else None

    def db(self):
        con = sqlite3.connect(self.home / "state.db")
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL);
            CREATE TABLE IF NOT EXISTS session_model_usage (
                session_id TEXT, task TEXT, billing_provider TEXT, model TEXT, last_seen REAL);
            """
        )
        return con


def _mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    return m


PACKAGE = "cache_guard_under_test"


def _seed_init_module():
    """pytest imports this folder's __init__.py as a module called `__init__` (the folder name is not a
    valid package name), with no parent package: the relative imports would fail. Hand it a loaded one."""
    spec = importlib.util.spec_from_file_location("__init__", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["__init__"] = module
    spec.loader.exec_module(module)


_seed_init_module()


class PluginView:
    """The plugin package and its modules as one object, so tests keep using `env.plugin._name`.

    Reading finds the name in the package, then in each module. Writing (monkeypatch.setattr) goes
    to the module that holds the name: that is the module whose code reads it.
    No instance __dict__: pytest's monkeypatch then saves the old value with getattr, so undo restores it.
    """

    __slots__ = ("_modules",)

    def __init__(self, package):
        object.__setattr__(self, "_modules", [package, *(
            m for m in vars(package).values()
            if isinstance(m, types.ModuleType) and m.__name__.startswith(package.__name__ + "."))])

    def __getattr__(self, name):
        for module in self._modules:
            if name in vars(module):
                return vars(module)[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        owners = [m for m in self._modules if name in vars(m)]
        if not owners or (len(owners) > 1 and isinstance(vars(owners[0])[name], types.ModuleType)):
            raise AttributeError(f"no single plugin module has {name}")  # patch the module you mean
        for module in owners:
            setattr(module, name, value)


def _forget_plugin():
    for name in [n for n in sys.modules if n == PACKAGE or n.startswith(PACKAGE + ".")]:
        del sys.modules[name]


def _load_plugin():
    """Load the plugin folder as a package, the way Hermes does (relative imports between its files)."""
    _forget_plugin()
    spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = package
    spec.loader.exec_module(package)
    return PluginView(package)


@pytest.fixture
def env(tmp_path, monkeypatch):
    plugin = _load_plugin()
    e = Env(tmp_path, plugin)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    def normalize(response_usage, *, provider=None, api_mode=None):
        raw = response_usage
        # Same shape as Hermes' CanonicalUsage: Chat totals include cache, prompt_tokens is the sum back.
        from types import SimpleNamespace
        read, write = raw.get("cache_read_tokens", 0), raw.get("cache_write_tokens", 0)
        inp = max(0, raw.get("prompt_tokens", 0) - read - write)
        return SimpleNamespace(input_tokens=inp, cache_read_tokens=read, cache_write_tokens=write,
                               prompt_tokens=inp + read + write)
    def await_decision(key, notify, data):
        """The gateway box: asks e.approval and answers the way Hermes does (a dict with resolved/choice)."""
        answer = e.approval(data["command"], data["description"],
                            allow_permanent=data["allow_permanent"], allow_session=data["allow_session"])
        if isinstance(answer, dict):
            return answer
        if answer == "timeout":
            return {"resolved": False, "choice": None}
        return {"resolved": True, "choice": answer}

    fakes = {
        "agent.usage_pricing": _mod("agent.usage_pricing", normalize_usage=normalize),
        "hermes_constants": _mod("hermes_constants", get_hermes_home=lambda: tmp_path),
        "hermes_cli": _mod("hermes_cli"),
        "hermes_cli.config": _mod("hermes_cli.config", read_raw_config_readonly=lambda: {}),
        "hermes_cli.middleware": _mod("hermes_cli.middleware", VALID_MIDDLEWARE={"llm_execution", "llm_request"}),
        "hermes_cli.plugins": _mod("hermes_cli.plugins", VALID_HOOKS={"transform_llm_output"}),
        "tools": _mod("tools"),
        "tools.approval": _mod("tools.approval", _gateway_notify_cb=lambda key: (lambda *a: None) if e.approval else None),
        "tools.approval_context": _mod("tools.approval_context", get_current_session_key=lambda default="": "key"),
        "tools.approval_gateway_wait": _mod("tools.approval_gateway_wait", _await_gateway_decision=await_decision),
    }
    def post_message(text):
        if isinstance(e.post_ok, Exception):
            raise e.post_ok
        if e.post_ok:
            e.posted.append(text)  # only what was really posted
        return types.SimpleNamespace(ok=bool(e.post_ok), error=None if e.post_ok else "no conversation")

    e.cron_run = None  # what ctx.current_cron_execution() answers
    plugin._CTX = types.SimpleNamespace(post_message=post_message, set_config=lambda key, value: None,
                                        get_config=lambda key, default=None: e.settings.get(key, default),
                                        current_cron_execution=lambda: e.cron_run)
    for name, mod in fakes.items():
        monkeypatch.setitem(sys.modules, name, mod)
    yield e
    _forget_plugin()


def big_request(tokens=60000):
    return {"messages": [{"role": "user", "content": "x" * (tokens * 4)}], "tools": []}


def run_middleware(env, *, session_id="s1", model="m1", provider="p", api_call_count=1,
                   platform="cli", request=None, **extra):
    """Invoke the llm_execution middleware; returns (result, provider_was_called, sentinel)."""
    called = []
    sentinel = object()

    def next_call():
        called.append(1)
        return sentinel

    out = env.plugin._on_llm_execution(
        next_call=next_call, request=request if request is not None else big_request(),
        session_id=session_id, model=model, provider=provider,
        api_call_count=api_call_count, platform=platform, api_mode="chat_completions", **extra,
    )
    return out, bool(called), sentinel


def put_session(plugin, session_id, record):
    """Write a session record the way the plugin stores it."""
    with plugin._storage_lock():
        plugin._sessions_housekeeping()
        plugin._write_json_atomic(plugin._session_file(session_id), record)


def append_observation(plugin, row):
    """Append one row to the observations journal."""
    with plugin._storage_lock():
        plugin._write_observation(row)


def seed_session(env, session_id="s1", ago=3600, model="p/m1", prompt_tokens=None, api_mode=None):
    data = {"last_api_ts": time.time() - ago, "model": model}
    if api_mode:
        data["api_mode"] = api_mode
    if prompt_tokens:
        data["prompt_tokens"] = prompt_tokens
    put_session(env.plugin, session_id, data)


def _cold_history(plugin, cold, warm, gap=3600):
    """Past comparable calls of p/m1 after a `gap` pause: `cold` lost the cache, `warm` kept it."""
    for i in range(2):  # two earlier positive reads: zero reads count as losses only after these
        append_observation(plugin, {
            "ts": 900 + i, "model": "p/m1", "api_mode": "anthropic",
            "gap_s": gap, "prompt_tokens": 50000,
            "cache_read_tokens": 50000, "cache_write_tokens": 0,
            "prev_prompt_tokens": None, "cause": "start",  # counts as a positive read, not as a measured pause
        })
    for i in range(cold + warm):
        lost = i < cold
        append_observation(plugin, {
            "ts": 1000 + i, "model": "p/m1", "api_mode": "anthropic",
            "gap_s": gap, "prompt_tokens": 50000,
            "cache_read_tokens": 0 if lost else 50000, "cache_write_tokens": 0,
            "prev_prompt_tokens": 50000, "cause": None,
        })


def _box_broke_notices(env):
    return [t for t in env.notices if "the cacheguard box failed" in t]


def _run_counting(env, platform="cli", session_id="s1"):
    """Like run_middleware, but counts provider calls."""
    calls = []
    sentinel = object()

    def next_call():
        calls.append(1)
        return sentinel

    out = env.plugin._on_llm_execution(
        next_call=next_call, request=big_request(), session_id=session_id, model="m1", provider="p",
        api_call_count=1, platform=platform, api_mode="chat_completions")
    return out, len(calls), sentinel


def _journal_row(ts=1.0, prompt=1000, **extra):
    row = {"ts": ts, "model": "p/m1", "api_mode": "chat_completions",
           "gap_s": 600.0, "prompt_tokens": prompt,
           "cache_read_tokens": prompt, "cache_write_tokens": 0,
           "prev_prompt_tokens": prompt, "cause": None}
    row.update(extra)
    return row


def exact_row(**overrides):
    data = {
        "ts": 10, "model": "p/m", "api_mode": "anthropic",
        "gap_s": 600, "prompt_tokens": 100,
        "cache_read_tokens": 95, "cache_write_tokens": 0,
        "prev_prompt_tokens": 100, "cause": "start",
    }
    data.update(overrides)
    return data


@pytest.fixture
def fake_provider():
    from e2e_support import FakeProvider
    provider = FakeProvider()
    yield provider
    provider.close()


@pytest.fixture
def hermes_python():
    from e2e_support import hermes_python
    return hermes_python()


@pytest.fixture
def make_hermes_home(fake_provider):
    """Factory for a temp Hermes home pointing at the fake provider; removed after the test."""
    from e2e_support import HermesHome
    homes = []

    def make(**settings):
        homes.append(HermesHome(fake_provider.url, settings))
        return homes[-1]

    yield make
    for h in homes:
        h.close()


@pytest.fixture
def webui(hermes_python, make_hermes_home):
    from e2e_support import WebUI
    home = make_hermes_home(desktop="block")  # the web UI is in the desktop group
    ui = WebUI(hermes_python, home)
    ui.home = home
    yield ui
    ui.close()


def pytest_collection_modifyitems(items):
    # The gateway contract file also runs as a script inside Hermes' Python (no pytest there),
    # so it is marked here instead of with a pytestmark in the file.
    for item in items:
        if item.path.name == "test_gateway_contract.py":
            item.add_marker(pytest.mark.e2e)
