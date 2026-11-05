"""Tests of health.py: symbol checks and degraded status."""
import sys

import pytest
from conftest import (
    run_middleware,
)


def test_config_degraded_status_survives_health_refresh(env, monkeypatch):
    env.config["pause_m"] = "bad"
    env.plugin._cfg()
    monkeypatch.setattr(env.plugin, "_health", lambda: {
        name: (True, "") for name in ("config_api", "hermes_version")
    })
    env.plugin._run_health()
    assert "health: degraded" in env.plugin._health_text()


def test_newer_hermes_alone_is_not_degraded(env, monkeypatch):
    # A version gap is information; only a failed real check makes the plugin degraded.
    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "9.0.0")
    monkeypatch.setattr(env.plugin, "_tested_version", lambda: "0.21.5")
    ok, detail = env.plugin._health()["hermes_version"]
    assert ok and detail == "9.0.0 (tested 0.21.5)"


def test_without_set_config_health_says_so(env, monkeypatch):
    monkeypatch.delattr(env.plugin._CTX, "set_config")
    env.plugin._run_health()
    assert env.plugin._HEALTH_EFFECTS["set_config"] in env.notices
    assert "set_config: missing or changed" in env.plugin._health_text()


def test_health_notices_show_no_internal_names(env):
    internal = ("state.db", "llm_execution", "degraded",
                "gateway_approval", "session_key", "Error", "_")
    for name, effect in env.plugin._HEALTH_EFFECTS.items():
        assert effect.endswith(".") and not any(word in effect for word in internal), name


def _fake_approval_modules(monkeypatch, notify=True, wait=True):
    import sys
    import types

    approval = types.ModuleType("tools.approval")
    if notify:
        approval._gateway_notify_cb = lambda session_key: None
    gate = types.ModuleType("tools.approval_gateway_wait")
    if wait:
        gate._await_gateway_decision = lambda *a, **k: {}
    monkeypatch.setitem(sys.modules, "tools.approval", approval)
    monkeypatch.setitem(sys.modules, "tools.approval_gateway_wait", gate)


# ------------------------------------------- health: private symbols ---


def test_health_passes_when_gateway_symbols_exist(env, monkeypatch):
    _fake_approval_modules(monkeypatch)
    assert env.plugin._health()["gateway_approval"] == (True, "")


@pytest.mark.parametrize("kw", [dict(notify=False), dict(wait=False)])
def test_health_flags_missing_gateway_symbol(env, monkeypatch, kw):
    _fake_approval_modules(monkeypatch, **kw)
    assert env.plugin._health()["gateway_approval"][0] is False
    env.plugin._run_health()
    text = env.plugin._health_text()
    assert "gateway_approval" in text and "cacheguard cannot show the box here: it only warns." in text
    assert "cacheguard cannot show the box here: it only warns." in env.plugin._H["noticed"]
    assert not any("gateway_approval" in line or "_await_gateway" in line for line in env.notices)


def test_health_skips_cli_object_symbols(env):
    assert not any("modal" in name or "lock" in name for name in env.plugin._health())


@pytest.mark.parametrize("name, symbol", [
    ("usage_api", "normalize_usage"), ("session_key", "get_current_session_key")])
def test_health_checks_the_other_hermes_symbols(env, monkeypatch, name, symbol):
    monkeypatch.setattr(env.plugin, "_has_symbol", lambda mod, sym: True)
    assert env.plugin._health()[name][0] is True
    monkeypatch.setattr(env.plugin, "_has_symbol", lambda mod, sym: sym != symbol)
    assert env.plugin._health()[name][0] is False
    env.plugin._run_health()
    assert name in env.plugin._health_text()


def _fake_hermes_module(tmp_path, monkeypatch, source, pkg="cgfake_pkg"):
    """Write pkg/sub/mod.py with `source` on sys.path; the import would leave a marker in sys.modules."""
    mod = tmp_path / "fake_site" / pkg / "sub"
    mod.mkdir(parents=True)
    (tmp_path / "fake_site" / pkg / "__init__.py").write_text("import sys\nsys.modules['cgfake_marker'] = 1\n")
    (mod / "__init__.py").write_text("")
    (mod / "mod.py").write_text("import sys\nsys.modules['cgfake_marker'] = 1\n" + source)
    monkeypatch.syspath_prepend(str(tmp_path / "fake_site"))
    return f"{pkg}.sub.mod"


def test_symbol_only_in_a_comment_or_string_fails(env, tmp_path, monkeypatch):
    module = _fake_hermes_module(tmp_path, monkeypatch, "# def renamed_away(): pass\nNOTE = 'def old_name(): pass'\n")
    assert env.plugin._has_symbol(module, "renamed_away") is False
    assert env.plugin._has_symbol(module, "old_name") is False


def test_real_definitions_pass_the_symbol_check(env, tmp_path, monkeypatch):
    module = _fake_hermes_module(tmp_path, monkeypatch, (
        "def a(): pass\n"
        "async def b(): pass\n"
        "class C:\n    def method(self): pass\n"
        "D = 1\n"
        "try:\n    E = 2\nexcept Exception:\n    E = 3\n"
        "from os import path as F\n"))
    for name in ("a", "b", "C", "D", "E", "F", "C.method"):
        assert env.plugin._has_symbol(module, name) is True, name
    assert env.plugin._has_symbol(module, "C.missing") is False
    assert env.plugin._has_symbol(module, "method") is False  # a method is not a module-level name


def test_symbol_check_of_a_missing_module_fails(env):
    assert env.plugin._has_symbol("cgfake_nowhere.sub.mod", "x") is False
    assert env.plugin._has_symbol("cgfake_nowhere", "x") is False


def test_symbol_check_does_not_import_the_module(env, tmp_path, monkeypatch):
    module = _fake_hermes_module(tmp_path, monkeypatch, "def a(): pass\n")
    assert env.plugin._has_symbol(module, "a") is True
    assert "cgfake_marker" not in sys.modules
    assert not any(name.startswith("cgfake_pkg") for name in sys.modules)


def test_symbol_check_of_a_loaded_module_asks_the_module(env):
    assert env.plugin._has_symbol("tools.approval", "_gateway_notify_cb") is True
    assert env.plugin._has_symbol("tools.approval", "gone") is False


@pytest.mark.parametrize("name, module, table", [
    ("middleware", "hermes_cli.middleware", "VALID_MIDDLEWARE"),
    ("llm_request", "hermes_cli.middleware", "VALID_MIDDLEWARE"),
    ("transform_llm_output", "hermes_cli.plugins", "VALID_HOOKS"),
])
def test_health_flags_a_middleware_or_hook_name_hermes_no_longer_lists(env, monkeypatch, name, module, table):
    import sys

    assert env.plugin._health()[name] == (True, "")
    monkeypatch.setattr(sys.modules[module], table, set())
    assert env.plugin._health()[name][0] is False
    env.plugin._run_health()
    assert env.plugin._HEALTH_EFFECTS[name] in env.plugin._health_text()


_NEW_CHECKS = [
    ("single_query", "tools.approval_context", "_is_single_query_approval_context"),
]


@pytest.mark.parametrize("name, module, symbol", _NEW_CHECKS)
def test_each_new_health_check_is_listed_and_detects_a_rename(env, monkeypatch, name, module, symbol):
    assert name in env.plugin._HEALTH_EFFECTS
    seen = []
    monkeypatch.setattr(env.plugin, "_has_symbol", lambda mod, sym: seen.append((mod, sym)) or True)
    assert env.plugin._health()[name] == (True, "")
    assert (module, symbol) in seen
    monkeypatch.setattr(env.plugin, "_has_symbol", lambda mod, sym: (mod, sym) != (module, symbol))
    assert env.plugin._health()[name][0] is False
    env.plugin._run_health()
    assert name in env.plugin._health_text() and env.plugin._HEALTH_EFFECTS[name] in env.plugin._health_text()


def test_renamed_symbol_prints_a_degraded_line_and_the_message_is_still_sent(env, monkeypatch):
    monkeypatch.setattr(env.plugin, "_has_symbol", lambda mod, sym: sym != "get_current_session_key")
    env.plugin._run_health()
    assert any(line.endswith("cacheguard cannot show the box here: it only warns.") for line in env.notices)
    assert not any("degraded" in line or "session_key" in line for line in env.notices)
    _out, called, _sentinel = run_middleware(env, request={"messages": [{"role": "user", "content": "hi"}], "tools": []})
    assert called is True


def test_cron_run_check_reads_the_context_and_fails_open(env, monkeypatch):
    assert env.plugin._health()["cron_execution"] == (True, "")
    assert "cron_execution" in env.plugin._HEALTH_EFFECTS
    monkeypatch.delattr(env.plugin._CTX, "current_cron_execution")
    assert env.plugin._health()["cron_execution"][0] is False
    assert env.plugin._in_cron_run() is False  # a missing answer is "not cron", never an error
    env.plugin._CTX = None
    assert env.plugin._in_cron_run() is False
