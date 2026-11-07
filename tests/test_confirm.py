"""Tests of confirm.py: pause text, block text, single-query marker."""
import re

import pytest
from conftest import _cold_history


@pytest.mark.parametrize("seconds,text", [
    (30, "30 seconds"), (60, "1 minute"), (3600, "60 minutes"), (179 * 60, "179 minutes"),
    (5 * 3600, "5 hours"), (47 * 3600, "47 hours"), (5 * 86400, "5 days"),
])
def test_pause_text_uses_hours_and_days_for_long_pauses(env, seconds, text):
    assert env.plugin._pause_text(seconds) == text


@pytest.mark.parametrize("value, expected", [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("0", False), ("", False), ("no", False),
])
def test_single_query_env_fallback(env, monkeypatch, value, expected):
    # No tools.approval_context in the fakes: the import fails and the env marker decides.
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", value)
    assert env.plugin._single_query() is expected


# --- block text ---------------------------------------------------------------- #

PAUSE = "[CACHEGUARD] BLOCKED · 13:05 · 1 minute pause 🕐 · ~31,521 tokens may be rebilled"
RETRY = "To send it anyway: /retry, or send it again."
NEW = "Or start /new and paste it."
ATTACH = "To send it anyway: attach the image again and send it again."


@pytest.fixture(autouse=True)
def _fixed_clock(env, monkeypatch):
    monkeypatch.setattr(env.plugin, "_clock", lambda: "13:05")


def _text(env, **kw):
    args = dict(size=31521, model="p/m1", prev=None, gap_s=70.0, session_id="local-6f04")
    args.update(kw)
    return env.plugin._block_text(args["size"], args["model"], args["prev"], args["gap_s"],
                                  args["session_id"], args.get("image", False), args.get("tiny", False))


def test_clock_is_hours_and_minutes(env, monkeypatch):
    monkeypatch.undo()  # the real clock, not the fixed one
    assert re.fullmatch(r"\d\d:\d\d", env.plugin._clock())


def test_block_text_pause_is_three_lines(env):
    assert _text(env).split("\n") == [PAUSE, RETRY, NEW + " Session ID: local-6f04"]


def test_block_text_pause_has_the_loss_counts_when_known(env):
    _cold_history(env.plugin, cold=6, warm=4, gap=70)
    assert _text(env).split("\n")[0] == PAUSE + " · lost 6 of 10 similar pauses"


def test_block_text_model_change(env):
    lines = _text(env, prev="p/old", model="p/new", gap_s=None).split("\n")
    assert lines[0] == "[CACHEGUARD] BLOCKED · 13:05 · model changed p/old → p/new · ~31,521 tokens may be rebilled"
    assert lines[1:] == [RETRY, NEW + " Session ID: local-6f04"]


def test_block_text_image_says_to_attach_it_again(env):
    assert _text(env, image=True).split("\n")[1:] == [ATTACH, NEW + " Session ID: local-6f04"]


def test_block_text_without_session_id(env):
    assert _text(env, session_id=None).split("\n")[2] == NEW


def test_block_text_session_id_stays_on_one_line(env):
    assert len(_text(env, session_id="a\nb").split("\n")) == 3


@pytest.mark.parametrize("kw,text", [
    ({}, "1m 🕐 · ~32kt"),
    ({"size": 999}, "1m 🕐 · ~999t"),
    ({"size": 1_234_567}, "1m 🕐 · ~1.2Mt"),
    ({"gap_s": 7500.0}, "2h05 🕐 · ~32kt"),
    ({"prev": "p/old", "model": "p/new", "gap_s": None}, "p/old → p/new · ~32kt"),
    ({"image": True}, "1m 🕐 · ~32kt · attach the image again"),
    ({"prev": "p/old", "model": "p/new", "gap_s": None, "image": True}, "p/old → p/new · ~32kt · attach the image again"),
])
def test_tiny_block_text_is_one_line(env, kw, text):
    assert _text(env, tiny=True, **kw) == "[CACHEGUARD] BLOCKED · 13:05 · " + text


def test_tiny_block_text_has_the_loss_counts_when_known(env):
    _cold_history(env.plugin, cold=1, warm=62, gap=70)
    assert _text(env, tiny=True) == "[CACHEGUARD] BLOCKED · 13:05 · 1m 🕐 · ~32kt · 1/63"


@pytest.mark.parametrize("tiny", [False, True])
def test_both_block_texts_start_with_the_time(env, tiny):
    assert _text(env, tiny=tiny).startswith(env.plugin._BLOCKED_PREFIX + "13:05 · ")


@pytest.mark.parametrize("tiny", [False, True])
@pytest.mark.parametrize("kw", [{}, {"image": True}, {"prev": "p/old", "model": "p/new", "gap_s": None}])
def test_both_block_texts_are_recognized_by_history_cleaning(env, tiny, kw):
    text = _text(env, tiny=tiny, **kw)
    assert text.startswith(env.plugin._BLOCKED_PREFIX)
    assert env.plugin._is_blocked_reply({"role": "assistant", "content": text})
