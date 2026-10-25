"""Fakes for the Hermes modules the plugin imports lazily; no Hermes install needed."""
import importlib.util
import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


class Env:
    """Handle on the fake Hermes environment handed to tests."""

    def __init__(self, home, plugin):
        self.home = home
        self.plugin = plugin
        self.settings = {}        # plugins.entries.cacheguard.settings
        self.approval = None  # callable or None (no callback available)
        self.bot_managed = False
        self.write_origin = "foreground"  # Hermes' default skill write origin (main agent)
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
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, title TEXT, session_key TEXT, started_at REAL);
            """
        )
        return con


def _mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    return m


@pytest.fixture
def env(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("cache_guard_under_test", ROOT / "__init__.py")
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)

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
    fakes = {
        "agent.usage_pricing": _mod("agent.usage_pricing", normalize_usage=normalize),
        "hermes_constants": _mod("hermes_constants", get_hermes_home=lambda: tmp_path),
        "hermes_cli": _mod("hermes_cli"),
        "hermes_cli.config": _mod("hermes_cli.config", read_raw_config=lambda: {
            "plugins": {"entries": {"cacheguard": {"settings": dict(e.settings)}}},
        }),
        "tools": _mod("tools"),
        "tools.bot_mode_probe": _mod("tools.bot_mode_probe", BOT_CHAT_TITLE="Bot Chat",
                                      is_bot_mode_managed=lambda home=None: e.bot_managed),
        "tools.skill_provenance": _mod("tools.skill_provenance", get_current_write_origin=lambda: e.write_origin),
        "tools.terminal_tool": _mod("tools.terminal_tool", _get_approval_callback=lambda: e.approval),
    }
    def post_message(text):
        if isinstance(e.post_ok, Exception):
            raise e.post_ok
        if e.post_ok:
            e.posted.append(text)  # only what was really posted
        return types.SimpleNamespace(ok=bool(e.post_ok), error=None if e.post_ok else "no conversation")

    plugin._CTX = types.SimpleNamespace(post_message=post_message)
    for name, mod in fakes.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return e


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


def seed_session(env, session_id="s1", ago=3600, model="p/m1", prompt_tokens=None):
    data = {"last_api_ts": time.time() - ago, "model": model}
    if prompt_tokens:
        data["prompt_tokens"] = prompt_tokens
    put_session(env.plugin, session_id, data)


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
