"""Tests of config.py: settings, modes, defaults."""
import types

import pytest

EDGES = [60.0, 300.0, 1800.0, 7200.0, 86400.0]


def test_bucket_index(env):
    f = env.plugin._bucket_index
    assert f(0, EDGES) == 0
    assert f(59, EDGES) == 0
    assert f(60, EDGES) == 1
    assert f(299, EDGES) == 1
    assert f(86400, EDGES) == 5
    assert f(10**9, EDGES) == 5


def test_pause_m_invalid_in_config_falls_back_to_default(env):
    env.settings = {"pause_m": 0.5}
    assert env.plugin._cfg()["pause_m"] == 10
    assert "invalid pause_m" in env.plugin._health_text()


def test_invalid_config_uses_visible_defaults_and_keeps_block_mode(env):
    env.config = {
        "cli": "block", "pause_m": "oops",
    }
    cfg = env.plugin._cfg()
    assert cfg["cli"] == "block" and cfg["pause_m"] == 10
    assert "health: degraded" in env.plugin._health_text()


@pytest.mark.parametrize("broken", ["no ctx", "no get_config", "get_config raises"])
def test_unreadable_config_uses_defaults_and_says_so(env, broken):
    def raises(key, default=None):
        raise OSError("config.yaml unreadable")

    env.settings = {"cli": "block"}
    env.plugin._CTX = {"no ctx": None, "no get_config": object(),
                       "get_config raises": types.SimpleNamespace(get_config=raises)}[broken]
    cfg = env.plugin._cfg()
    assert cfg == env.plugin.DEFAULTS
    assert "configuration unavailable" in env.plugin._health_text()


def test_unbounded_config_number_falls_back_without_raising(env):
    env.config["pause_m"] = 10 ** 1000
    cfg = env.plugin._cfg()
    assert cfg["pause_m"] == 10
    assert "health: degraded" in env.plugin._health_text()


def test_defaults_keys_are_frozen(env):
    # A new setting needs the maintainer's explicit OK (AGENTS.md): change this list only when asked.
    assert sorted(env.plugin.DEFAULTS) == sorted([
        "cli", "desktop", "gateway", "display_on_success", "size_k", "pause_m", "journal_rows", "chat_files"])


@pytest.mark.parametrize("bad", [0, -5, 2.5, "many", True, "10000"])
@pytest.mark.parametrize("name,default", [("journal_rows", 10000), ("chat_files", 200)])
def test_bad_count_setting_falls_back_to_default_and_is_reported(env, name, default, bad):
    env.settings = {name: bad}
    assert env.plugin._cfg()[name] == default
    assert f"invalid {name}; using default" in env.plugin._health_text()


def test_cfg_asks_hermes_for_each_setting_by_name(env):
    asked = []
    env.settings = {"cli": "block"}
    ctx = env.plugin._CTX
    env.plugin._CTX = types.SimpleNamespace(get_config=lambda key, default=None: (asked.append(key), ctx.get_config(key, default))[1])
    assert env.plugin._cfg()["cli"] == "block"
    assert sorted(asked) == sorted(env.plugin.DEFAULTS)


@pytest.mark.parametrize("bad", ["yes", 1])
def test_bad_display_value_acts_as_false_and_is_reported(env, bad):
    env.config["display_on_success"] = bad
    assert env.plugin._cfg()["display_on_success"] is False
    assert "invalid display_on_success; using default" in env.plugin._H["config_errors"]
    assert "config: invalid display_on_success; using default" in env.plugin._health_text()


def test_desktop_platforms_use_desktop_mode_when_set(env):
    cfg = {"cli": "warn", "desktop": "block", "gateway": "off"}
    for platform in ("desktop", "webui"):
        assert env.plugin._surface_mode({"platform": platform}, cfg) == "block"
    for platform in ("cli", "tui", "local", "acp"):
        assert env.plugin._surface_mode({"platform": platform}, cfg) == "warn"
    assert env.plugin._surface_mode({"platform": "telegram"}, cfg) == "off"


def test_desktop_defaults_to_warn_whatever_cli_says(env):
    env.config = {"cli": "block"}
    cfg = env.plugin._cfg()
    assert cfg["desktop"] == "warn"
    for platform in ("desktop", "webui"):
        assert env.plugin._surface_mode({"platform": platform}, cfg) == "warn"
        assert env.plugin._is_cli_or_desktop({"platform": platform})


def test_invalid_desktop_value_is_rejected(env):
    env.config = {"cli": "block", "desktop": "maybe"}
    cfg = env.plugin._cfg()
    assert cfg["desktop"] == "warn" and cfg["cli"] == "block"
    assert "health: degraded" in env.plugin._health_text()
    env.config = {"desktop": "off"}
    assert env.plugin._cfg()["desktop"] == "off"


def test_mode_values_are_not_reshaped(env):
    env.config.update(cli=" Block ", gateway="WARN")
    cfg = env.plugin._cfg()
    assert (cfg["cli"], cfg["gateway"]) == ("warn", "off")  # defaults: neither value is a mode
    assert "health: degraded" in env.plugin._health_text()


def test_unquoted_yaml_off_means_off(env):
    import yaml
    raw = yaml.safe_load("plugins:\n  entries:\n    cacheguard:\n      settings:\n"
                         "        cli: off\n        desktop: off\n        gateway: off\n")
    env.settings.update(raw['plugins']['entries']['cacheguard']['settings'])
    assert env.settings['cli'] is False
    cfg = env.plugin._cfg()
    assert [cfg[k] for k in ('cli', 'desktop', 'gateway')] == ['off'] * 3
    assert not env.plugin._H['config_errors']
