"""Tests of commands.py: the /cacheguard command."""
import inspect
import re
import sys

import pytest
from conftest import (
    _cold_history,
    append_observation,
    seed_session,
)

# --------------------------------------------------------- /cacheguard -----


def test_cmd_empty_table(env):
    out = env.plugin._cmd_show()
    assert "0 measured pauses" in out and "calls recorded" not in out
    assert "no observations yet" in out
    assert "Cache lost, by pause length" in out and "historical" not in out
    assert out.count("(no observations yet)") == 1
    assert "predicted" not in out and "probability" not in out
    assert "Loss = at least 30%" not in out and "Each bucket is separate" not in out
    assert "2/5 = losses/calls" not in out
    assert out.splitlines()[0].startswith("cacheguard  ·  cli warn  ·  desktop warn  ·  gateway off  ·  pause ")
    assert "cli (terminal" not in out and "gateway (Telegram" not in out
    assert out.splitlines()[2].startswith("setup: /cacheguard setup all block 10 20") and out.splitlines()[4] == "help: /cacheguard help"
    assert out.splitlines()[3] == "set: /cacheguard set pause_m 1  (change only the pause, here 1 minute)"
    assert "settings: /cacheguard setup" not in out and "health:" not in out


def _table_lines(out):
    return [ln for ln in out.splitlines() if ln.startswith(("model", "p/m1"))]


def _surface(monkeypatch, **env):
    monkeypatch.delenv("HERMES_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("surface", ["telegram", "discord", "slack", "webui", "Telegram"])
def test_table_is_fenced_on_markdown_surfaces(env, monkeypatch, surface):
    _cold_history(env.plugin, cold=2, warm=3, gap=400)
    _surface(monkeypatch)
    lines = env.plugin._cmd_entry("", platform=surface).splitlines()
    start = lines.index("```")
    assert lines[start + 1].startswith("model") and lines[start + 2].startswith("p/m1")
    assert lines[start + 3] == "```" and lines.count("```") == 2
    assert lines.index("Cache lost, by pause length") < start


@pytest.mark.parametrize("platform, env_vars", [
    (None, {}),                                       # classic CLI process before Hermes names a surface
    ("", {"HERMES_INTERACTIVE": "1"}),                # classic CLI with the terminal flag
    ("cli", {}),
    ("tui", {}),
    ("desktop", {}),
    ("sms", {}),                                      # unknown renderer: stay plain
])
def test_table_stays_plain_on_raw_surfaces(env, monkeypatch, platform, env_vars):
    _cold_history(env.plugin, cold=2, warm=3, gap=400)
    _surface(monkeypatch, **env_vars)
    out = env.plugin._cmd_entry("", platform=platform)
    assert "```" not in out and len(_table_lines(out)) == 2


def test_webui_without_a_named_surface_is_a_markdown_surface(env, monkeypatch):
    import types
    _cold_history(env.plugin, cold=2, warm=3, gap=400)
    _surface(monkeypatch)
    fake = types.SimpleNamespace(execute_plugin_command=lambda command: "")
    monkeypatch.setitem(sys.modules, "api.commands", fake)
    assert "```" in env.plugin._cmd_show()
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")  # a terminal process is never the web UI
    assert "```" not in env.plugin._cmd_show()


def test_command_takes_the_platform_the_way_hermes_passes_it(env):
    # Hermes calls a handler with the keywords it names; `platform` must be one of them.
    import inspect
    params = inspect.signature(env.plugin._cmd_entry).parameters
    assert params["platform"].kind is inspect.Parameter.KEYWORD_ONLY


def test_cmd_one_row_hides_old_rows(env):
    append_observation(env.plugin, {"model": "p/old", "gap_s": 400.0, "bucket": 2, "cold": True})
    _cold_history(env.plugin, cold=2, warm=3, gap=400)
    out = env.plugin._cmd_show()
    assert out.splitlines()[0].endswith("5 measured pauses")
    assert "p/old" not in out and "historical" not in out
    data_lines = [ln for ln in out.splitlines() if ln.startswith("p/m1")]
    assert len(data_lines) == 1
    assert "2/5" in data_lines[0] and "%" not in data_lines[0]


def test_cmd_reset(env):
    p = env.plugin
    append_observation(p, {"model": "m", "gap_s": 1.0})
    seed_session(env)
    assert "Measurements and session tracking cleared. Settings kept." in p._cmd_show("reset")
    assert p._load_observations() == [] and p._get_session("s1") == {}


class _FakeCtx:
    def __init__(self, env, fail=False):
        self.env, self.fail, self.calls = env, fail, []

    def set_config(self, key, value):
        if self.fail:
            raise PermissionError("managed install")
        self.calls.append((key, value))
        self.env.settings[key] = value

    def get_config(self, key, default=None):
        return self.env.settings.get(key, default)


def _with_ctx(env, **kw):
    ctx = _FakeCtx(env, **kw)
    env.plugin._CTX = ctx
    return ctx


# ------------------------------------------------------ /cacheguard set -----


@pytest.mark.parametrize("args,expected", [
    ("cli block", ("cli", "block")),
    ("gateway warn", ("gateway", "warn")),
    ("desktop block", ("desktop", "block")),
    ("pause_m 10", ("pause_m", 10)),
    ("pause_m 1", ("pause_m", 1)),
    ("size_k 30", ("size_k", 30)),
    ("journal_rows 5000", ("journal_rows", 5000)),
    ("chat_files 50", ("chat_files", 50)),
])
def test_set_writes_and_takes_effect(env, args, expected):
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show("set " + args)
    assert "saved" in out and ctx.calls == [expected]
    assert env.plugin._cfg()[expected[0]] == expected[1]


@pytest.mark.parametrize("args", ["", "cli", "cli maybe", "bot warn", "gateway maybe", "pause_m abc", "pause_m -3", "size_k", "bogus 1", "minutes 10", "minutes 1", "pause_minutes 10", "tokens 30", "cli block extra",
                                  "journal_rows 0", "journal_rows 2.5", "journal_rows 1e4", "chat_files -1", "chat_files x"])
def test_set_rejects_bad_input_without_writing(env, args):
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show(("set " + args).strip())
    assert "usage:" in out and ctx.calls == []


@pytest.mark.parametrize("args", ["pause_m 0", "pause_m 0.5"])
def test_set_minutes_below_one_is_refused(env, args):
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show("set " + args)
    assert out.startswith("cacheguard: pause_m must be a number >= 1.") and "usage:" in out and ctx.calls == []


@pytest.mark.parametrize("args", [
    "pause_m nan", "pause_m inf", "pause_m -inf", "pause_m 1e308",
    "size_k nan", "size_k inf", "size_k -inf", "size_k 1e308",
])
def test_set_rejects_nonfinite_and_overflow_without_writing(env, args):
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show("set " + args)
    assert "must be a number" in out or "is in thousands" in out
    assert ctx.calls == []


def test_set_and_setup_refuse_size_above_2000_thousands(env):
    ctx = _with_ctx(env)
    msg = "cacheguard: size_k is in thousands: 20 = 20k. A number from 0 to 2000."
    for cmd in ("set size_k 20000", "set size_k 2001", "setup all block 10 20000"):
        out = env.plugin._cmd_show(cmd)
        assert msg in out and "usage:" in out and ctx.calls == [], cmd
    assert "saved" in env.plugin._cmd_show("set size_k 2000")
    assert ctx.calls == [("size_k", 2000)]


def test_set_write_failure_is_reported_not_raised(env):
    _with_ctx(env, fail=True)
    assert "could not write" in env.plugin._cmd_show("set cli block")


def _fake_settings_writer(env):
    """Records the (key, value) pairs written through ctx.set_config."""
    return _with_ctx(env).calls


def test_set_without_context_is_reported_not_raised(env):
    env.plugin._CTX = None
    assert "could not write" in env.plugin._cmd_show("set cli block")


def test_set_writes_through_the_context(env, monkeypatch):
    saved = _fake_settings_writer(env)
    out = env.plugin._cmd_show("set cli block")
    assert "saved" in out and saved == [("cli", "block")]


def test_summary_shows_health_only_when_degraded(env):
    assert "health:" not in env.plugin._cmd_show()
    env.plugin._H["checks"]["usage_api"] = (False, "missing or changed")
    assert "health: degraded" in env.plugin._cmd_show()


def test_summary_waits_for_enough_measurements(env):
    _cold_history(env.plugin, cold=1, warm=1)
    out = env.plugin._cmd_show()
    assert "(2 measurements so far, need at least 5)" in out
    assert "p/m1" not in out


def test_summary_shows_help_line(env):
    assert "help: /cacheguard help" in env.plugin._cmd_show()


@pytest.mark.parametrize("args", ["help", "HELP"])
def test_help_text_for_help(env, args):
    assert env.plugin._cmd_show(args) == env.plugin._HELP_TEXT


def test_help_lists_every_command(env):
    text = env.plugin._HELP_TEXT
    for line in ("/cacheguard all","/cacheguard <model>", "/cacheguard setup",
                 "/cacheguard set <name>", "/cacheguard reset",
                 "/cacheguard help"):
        assert line in text


def test_cacheguard_subcommands_are_frozen(env):
    # A new command or alias needs the maintainer's explicit OK (AGENTS.md): change this list only when asked.
    src = inspect.getsource(env.plugin._cmd_show)
    assert set(re.findall(r'== "(\w+)"', src)) == {"help", "set", "setup", "reset", "all"}
    assert '"?"' not in src and '"clear"' not in src


def test_help_covers_every_subcommand_handled_by_show(env):
    # Any new `/cacheguard <word>` branch in _cmd_show must be documented in help.
    src = inspect.getsource(env.plugin._cmd_show)
    words = set(re.findall(r'== "(\w+)"', src)) | set(re.findall(r'"(reset)"', src))
    assert {"set", "setup", "reset", "all"} <= words
    for word in words:
        assert f"/cacheguard {word}" in env.plugin._HELP_TEXT


def test_help_lists_every_set_name(env):
    samples = {"cli": "block", "desktop": "block", "display_on_success": "on", "block_images": "on", "tiny_block": "on", "gateway": "block",
               "pause_m": "5", "size_k": "30", "journal_rows": "5000", "chat_files": "50"}
    for name, raw in samples.items():
        assert env.plugin._parse_setting(name, raw)[0] is not None
        assert f"  {name} " in env.plugin._HELP_TEXT


@pytest.mark.parametrize("args", ["pause_m -1", "pause_m 0", "pause_m 0.5"])
def test_minutes_below_one_gets_the_help_wording(env, args):
    out = env.plugin._cmd_show("set " + args)
    assert out.startswith("cacheguard: pause_m must be a number >= 1.")
    assert "number >= 1" in env.plugin._HELP_TEXT


def test_setup_all_sets_all_three_groups(env):
    env.settings = {"cli": "warn", "gateway": "warn"}
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show("setup all block 10 30")
    assert ctx.calls == [("cli", "block"), ("desktop", "block"), ("gateway", "block"),
                         ("pause_m", 10), ("size_k", 30)]
    assert out == ("cacheguard: cli = block · desktop = block · gateway = block · pause ≥ 10 min · size ≥ 30k "
                   "(pause and size apply to all groups; saved to config).")


@pytest.mark.parametrize("group", ["cli", "desktop", "gateway"])
def test_setup_with_group_sets_only_that_group(env, group):
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show(f"setup {group} warn 5 20")
    assert ctx.calls == [(group, "warn"), ("pause_m", 5), ("size_k", 20)]
    assert out == (f"cacheguard: {group} = warn · pause ≥ 5 min · size ≥ 20k "
                   "(pause and size apply to all groups; saved to config).")


def test_set_cli_names_the_key_it_wrote(env):
    _with_ctx(env)
    out = env.plugin._cmd_show("set cli warn")
    assert out == "cacheguard: cli = warn (saved to config)."


@pytest.mark.parametrize("args", [
    "", "block 10 30", "bogus block", "all maybe 10 30", "all block 10 30 extra",
    "all block 10", "cli block 30",                                 # only one of pause, size
    "all block abc 30", "all block nan 30", "all block 0.5 30", "all block 10 -1", "all block 10 20000",
])
def test_setup_invalid_input_writes_nothing_and_shows_usage(env, args):
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show(("setup " + args).strip())
    assert "usage:" in out and ctx.calls == []


def test_setup_mode_only_keeps_pause_and_size(env):
    env.settings = {"pause_m": 10, "size_k": 30}
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show("setup cli block")
    assert ctx.calls == [("cli", "block")]
    assert env.plugin._cfg()["pause_m"] == 10 and env.plugin._cfg()["size_k"] == 30
    assert "pause" not in out and "context" not in out and out.endswith("(saved to config).")


def test_reset_with_extra_arguments_is_refused(env, monkeypatch):
    saved = _fake_settings_writer(env)
    assert env.plugin._cmd_show("reset now") == "usage: /cacheguard reset"
    assert saved == []


def test_unknown_arguments_are_named(env):
    _seed_rows(env, ["p/model-a"], per=5)
    unknown = "unknown argument '{}'. Try /cacheguard help"
    assert env.plugin._cmd_show("all foo bar") == unknown.format("foo")
    assert env.plugin._cmd_show("foo") == (
        "No recorded model matches foo. Use /cacheguard all to list recorded models.")
    assert env.plugin._cmd_show("model-a foo") == unknown.format("foo")
    assert env.plugin._cmd_show("help me") == unknown.format("me")
    assert "p/model-a" in env.plugin._cmd_show("MODEL-A")  # a recorded model still filters
    assert "p/model-a" in env.plugin._cmd_show("all")


def test_bad_input_on_every_subcommand_gets_usage(env):
    assert env.plugin._cmd_show("setup").startswith("usage: /cacheguard setup")
    assert env.plugin._cmd_show("setup nope warn").startswith(
        "cacheguard: Unknown group nope; use all, cli, desktop or gateway.")
    assert env.plugin._cmd_show("set").startswith("usage: /cacheguard set")
    assert env.plugin._cmd_show("set cli maybe").startswith("cacheguard: cli must be")


def test_set_display_on_success_is_saved(env, monkeypatch):
    ctx = _with_ctx(env)
    assert "saved" in env.plugin._cmd_show("set display_on_success on")
    assert "saved" in env.plugin._cmd_show("set display_on_success off")
    assert ctx.calls == [("display_on_success", True), ("display_on_success", False)]
    assert env.settings["display_on_success"] is False
    saved = _fake_settings_writer(env)
    env.plugin._cmd_show("set display_on_success on")
    assert saved == [("display_on_success", True)]


def test_set_tiny_block_is_saved(env):
    ctx = _with_ctx(env)
    assert "saved" in env.plugin._cmd_show("set tiny_block on")
    assert "saved" in env.plugin._cmd_show("set tiny_block off")
    assert ctx.calls == [("tiny_block", True), ("tiny_block", False)]
    assert env.plugin._cmd_show("set tiny_block maybe").startswith("cacheguard: tiny_block must be on or off.")


def test_set_block_images_is_saved(env):
    ctx = _with_ctx(env)
    assert "saved" in env.plugin._cmd_show("set block_images on")
    assert "saved" in env.plugin._cmd_show("set block_images off")
    assert ctx.calls == [("block_images", True), ("block_images", False)]
    assert env.plugin._cmd_show("set block_images maybe").startswith("cacheguard: block_images must be on or off.")


def test_set_display_on_success_refuses_true(env):
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show("set display_on_success true")
    assert out.startswith("cacheguard: display_on_success must be on or off.")
    assert ctx.calls == []


def _seed_rows(env, models, per=1):
    for m in models:
        for i in range(per):
            append_observation(env.plugin, {"ts": i, "session_id": "x", "model": m, "api_mode": "anthropic",
                                            "api_request_id": f"{m}-{i}", "gap_s": 400.0,
                                            "prompt_tokens": 1000, "cache_read_tokens": 900,
                                            "cache_write_tokens": 0, "prev_prompt_tokens": 1000,
                                            "cause": None})


def test_show_docs_first_and_top_rows(env):
    _seed_rows(env, [f"p/model-{i}" for i in range(15)])
    out = env.plugin._cmd_show("")
    assert out.index("help:") < out.index("Cache lost, by pause length")
    assert sum("p/model-" in line for line in out.splitlines()) == 10
    assert "5 more models: /cacheguard all" in out


def test_show_all_and_filter(env):
    _seed_rows(env, [f"p/model-{i}" for i in range(15)] + ["openrouter/deepseek/v4"], per=5)
    assert sum("p/model-" in line for line in env.plugin._cmd_show("all").splitlines()) == 15
    out = env.plugin._cmd_show("DeepSeek")
    assert "openrouter/deepseek/v4" in out and "p/model-" not in out
    assert env.plugin._cmd_show("zzz") == (
        "No recorded model matches zzz. Use /cacheguard all to list recorded models.")


def test_show_observed_counts_for_its_own_range(env):
    def call(i, gap, read):
        append_observation(env.plugin, {"ts": i, "session_id": "x", "model": "p/m", "api_mode": "anthropic",
                                        "api_request_id": str(i), "gap_s": gap, "prompt_tokens": 1000,
                                        "cache_read_tokens": read, "cache_write_tokens": 0,
                                        "prev_prompt_tokens": 1000, "cause": None})
    for i in range(20):
        call(i, 100.0, 900)
    call(20, 400.0, 0)
    out = env.plugin._cmd_show("")
    rows = [line for line in out.splitlines() if line.startswith("p/m")]
    assert len(rows) == 1 and "0/20" in rows[0]
    assert "1/1" in rows[0] and "tokens unknown" not in rows[0]
    assert "predicted" not in out


def test_parse_setting_group_modes(env):
    assert env.plugin._parse_setting("cli", "warn")[0] == ("cli", "warn", "warn")
    assert env.plugin._parse_setting("desktop", "block")[0] == ("desktop", "block", "block")
    assert env.plugin._parse_setting("desktop", "maybe")[0] is None
    for gone in ("show_model", "model_change", "max_observations"):
        assert env.plugin._parse_setting(gone, "on")[0] is None


def test_superscript_digits_in_command_args_do_not_crash(env):
    assert "number" in env.plugin._cmd_show("set tokens ²")


def test_help_example_sets_one_group(env):
    assert "/cacheguard setup cli block 10 20" in env.plugin._HELP_TEXT
    assert "setup all block 10 30" not in env.plugin._HELP_TEXT
