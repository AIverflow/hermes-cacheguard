import inspect
import json
import os
import stat
from pathlib import Path
import re
import sys
import time
import types

import pytest

from conftest import append_observation, big_request, put_session, run_middleware, seed_session

EDGES = [60.0, 300.0, 1800.0, 7200.0, 86400.0]


# ---------------------------------------------------------------- maths ----
def test_bucket_index(env):
    f = env.plugin._bucket_index
    assert f(0, EDGES) == 0
    assert f(59, EDGES) == 0
    assert f(60, EDGES) == 1
    assert f(299, EDGES) == 1
    assert f(86400, EDGES) == 5
    assert f(10**9, EDGES) == 5


# ------------------------------------------------------------- formatting --
def test_warning_shows_nothing_before_the_reply(env):
    seed_session(env, ago=3600)
    run_middleware(env)
    assert not env.notices and env.warned() is not None


def test_confirmation_without_enough_data_has_no_probability(env):
    env.config["cli"] = "block"
    seed_session(env, ago=3600)
    prompts = []
    env.approval = lambda command, desc, **kw: (prompts.append(command), "deny")[1]
    out, called, _ = run_middleware(env)
    assert not called and "message not sent" in out.choices[0].message.content
    assert "%" not in prompts[0] and "est." not in prompts[0]


def _cold_history(plugin, cold, warm, gap=3600):
    """Past comparable calls of p/m1 after a `gap` pause: `cold` lost the cache, `warm` kept it."""
    for i in range(3):  # three earlier positive reads: zero reads count as losses only after these
        append_observation(plugin, {
            "ts": 900 + i, "session_id": f"w{i}", "model": "p/m1", "api_mode": "anthropic",
            "api_request_id": f"w{i}", "gap_s": 0, "prompt_tokens": 50000,
            "cache_read_tokens": 50000, "cache_write_tokens": 0,
            "prev_prompt_tokens": 50000, "cause": None,
        })
    for i in range(cold + warm):
        lost = i < cold
        append_observation(plugin, {
            "ts": 1000 + i, "session_id": f"h{i}", "model": "p/m1", "api_mode": "anthropic",
            "api_request_id": str(i), "gap_s": gap, "prompt_tokens": 50000,
            "cache_read_tokens": 0 if lost else 50000, "cache_write_tokens": 0,
            "prev_prompt_tokens": 50000, "cause": None,
        })


def test_confirmation_shows_counts_with_enough_data(env):
    env.config["cli"] = "block"
    seed_session(env, ago=3600)
    _cold_history(env.plugin, cold=6, warm=4)
    prompts = []
    env.approval = lambda command, desc, **kw: (prompts.append(command), "deny")[1]
    run_middleware(env)
    assert "Paused 60 minutes · ~" in prompts[0] and "tokens may be rebilled · lost 6 of 10 similar pauses" in prompts[0]


# -------------------------------------------------------------- idle trigger
def test_idle_trigger_warns_and_sends(env):
    seed_session(env, ago=3600)
    out, called, sentinel = run_middleware(env)
    assert called and out is sentinel
    assert not env.notices and env.warned() is not None


def test_gateway_default_off_still_records_usage(env):
    seed_session(env, ago=3600)
    out, called, sentinel = run_middleware(env, platform="telegram")
    assert called and out is sentinel and env.warned() is None
    assert env.plugin._load_observations()


@pytest.mark.parametrize("platform,setting", [("cli", "cli"), ("telegram", "gateway")])
@pytest.mark.parametrize("mode", ["off", "warn", "block"])
def test_surface_modes_gate_idle_trigger(env, monkeypatch, platform, setting, mode):
    env.config.update({setting: mode, "size_k": 1})
    seed_session(env, ago=3600)
    if mode == "block" and platform == "cli":
        monkeypatch.setattr(env.plugin, "_healthy", lambda _name: True)
        env.approval = lambda *a, **k: "deny"
    if mode == "block" and platform != "cli":
        monkeypatch.setattr(env.plugin, "_gateway_approval", lambda: ("key", lambda *a: None))
        monkeypatch.setattr(env.plugin, "_gateway_confirmation", lambda *a: object())
    out, called, sentinel = run_middleware(env, platform=platform)
    if mode == "off":
        assert called and out is sentinel and env.warned() is None
    elif mode == "warn":
        assert called and out is sentinel
    elif platform == "cli":
        assert not called and "message not sent" in out.choices[0].message.content
    else:
        assert not called


@pytest.mark.parametrize("mode", ["warn", "block"])
@pytest.mark.parametrize("platform", ["cli", "telegram"])
def test_bot_chat_is_pure_pass_through(env, platform, mode):
    env.bot_managed = True
    env.config.update(cli=mode, gateway=mode, size_k=1)
    env.approval = lambda *a, **k: pytest.fail("Bot Chat must never prompt")
    con = env.db()
    con.execute("INSERT INTO sessions(id,title) VALUES('s1','Bot Chat')")
    con.commit()
    con.close()
    seed_session(env, ago=3600)
    before = env.plugin._read_session("s1")
    out, called, sentinel = run_middleware(env, platform=platform, model="new")
    assert called and out is sentinel
    assert env.warned() is None and "s1" not in env.plugin._PENDING
    assert env.plugin._read_observations() == [] and env.plugin._read_session("s1") == before
    env.plugin._PENDING["s1"] = "[CACHEGUARD] stale"
    assert env.plugin._transform_llm_output(
        response_text="hi", session_id="s1", platform=platform) is None


def test_ordinary_gateway_chat_on_managed_profile_uses_gateway_mode(env, monkeypatch):
    env.bot_managed = True
    env.config.update(cli="off", gateway="block", size_k=1)
    con = env.db()
    con.execute("INSERT INTO sessions(id,title) VALUES('s1','ordinary chat')")
    con.commit()
    con.close()
    seed_session(env, ago=3600)
    blocked = object()
    monkeypatch.setattr(env.plugin, "_gateway_approval", lambda: ("key", lambda *a: None))
    monkeypatch.setattr(env.plugin, "_gateway_confirmation", lambda *a: blocked)
    out, called, _ = run_middleware(env, platform="telegram")
    assert out is blocked and not called


@pytest.mark.parametrize("trigger", ["idle", "model_change"])
def test_gateway_warn_mode_is_nonblocking(env, trigger):
    env.config.update(gateway="warn", cli="off", size_k=1)
    if trigger == "idle":
        seed_session(env, ago=3600)
    else:
        seed_session(env, ago=10, model="p/old")
    out, called, sentinel = run_middleware(env, platform="telegram", model="new")
    assert called and out is sentinel and env.warned() is not None


@pytest.mark.parametrize("title,managed,expected", [
    ("Bot Chat", True, True), ("Bot Chat", False, False), ("ordinary chat", True, False),
])
def test_bot_chat_identity_uses_exact_title_and_managed_install(env, title, managed, expected):
    env.bot_managed = managed
    con = env.db()
    con.execute("INSERT INTO sessions(id,title) VALUES('s1',?)", (title,))
    con.commit()
    con.close()
    assert env.plugin._is_bot_chat("s1") is expected


def test_bot_chat_answer_is_cached_per_session(env, monkeypatch):
    env.db().close()
    real = __import__("sqlite3").connect
    opened = []
    monkeypatch.setattr(__import__("sqlite3"), "connect", lambda *a, **k: opened.append(a) or real(*a, **k))
    assert env.plugin._is_bot_chat("s1") is False
    assert env.plugin._is_bot_chat("s1") is False
    assert len(opened) == 1
    assert env.plugin._is_bot_chat("s2") is False
    assert len(opened) == 2


def test_bot_chat_identity_reads_active_profile_database(env, monkeypatch, tmp_path):
    env.bot_managed = True
    home_a, home_b = tmp_path / "profiles" / "a", tmp_path / "profiles" / "b"
    home_a.mkdir(parents=True)
    home_b.mkdir(parents=True)
    for home, title in ((home_a, "Bot Chat"), (home_b, "ordinary chat")):
        con = __import__("sqlite3").connect(home / "state.db")
        con.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT)")
        con.execute("INSERT INTO sessions VALUES('same-id',?)", (title,))
        con.commit()
        con.close()
    hermes_constants = __import__("sys").modules["hermes_constants"]
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home_a)
    assert env.plugin._is_bot_chat("same-id")
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home_b)
    assert not env.plugin._is_bot_chat("same-id")


@pytest.mark.parametrize("trigger", ["idle", "model_change"])
def test_gateway_block_mode_retains_approval_for_both_triggers(env, monkeypatch, trigger):
    env.config.update(gateway="block", cli="off")
    if trigger == "idle":
        seed_session(env, ago=3600)
    else:
        seed_session(env, ago=10, model="p/old")
    blocked = object()
    monkeypatch.setattr(env.plugin, "_gateway_approval", lambda: ("key", lambda *a: None))
    monkeypatch.setattr(env.plugin, "_gateway_confirmation", lambda *a: blocked)
    out, called, _ = run_middleware(env, platform="telegram", model="new")
    assert out is blocked and not called


@pytest.mark.parametrize("trigger", ["idle", "model_change"])
def test_tui_block_uses_gateway_approval_when_no_cli_callback(env, monkeypatch, trigger):
    # The TUI registers no CLI approval callback; it asks through the gateway approval queue.
    env.config.update(cli="block", size_k=1)
    if trigger == "idle":
        seed_session(env, ago=3600)
    else:
        seed_session(env, ago=10, model="p/old")
    monkeypatch.setattr(env.plugin, "_healthy", lambda _name: True)
    blocked = object()
    monkeypatch.setattr(env.plugin, "_gateway_approval", lambda: ("key", lambda *a: None))
    monkeypatch.setattr(env.plugin, "_gateway_confirmation", lambda *a: blocked)
    out, called, _ = run_middleware(env, platform="tui", model="new")
    assert out is blocked and not called


MINTED = "3f2b8c1e-7a4d-4e6b-9c1a-0d5e2f8a1b34"  # Hermes mints a uuid4 when run_conversation gets no task_id


def _resume_quiet(env, task_id=MINTED, **extra):
    # `hermes chat -c NAME -Q` / `hermes -z -c NAME` / Bot Chat: resumed session, minted task id.
    env.config.update(size_k=1)
    seed_session(env, ago=3600)
    extra.setdefault("api_request_id", f"s1:{task_id}:abcd1234:api:1")
    return run_middleware(env, task_id=task_id, **extra)


def test_quiet_resume_with_minted_task_id_is_guarded(env):
    out, called, sentinel = _resume_quiet(env)
    assert called and out is sentinel and env.warned() is not None
    rows = env.plugin._load_observations()
    assert rows[-1].get("cause") != "auxiliary" and not env.plugin._is_auxiliary(rows[-1])


@pytest.mark.parametrize("origin", ["background_review", "side_question"])
def test_minted_task_id_fork_stays_auxiliary(env, origin):
    # Background review, curator and /btw forks share the session and also get a minted task id.
    env.write_origin = origin
    out, called, sentinel = _resume_quiet(env)
    assert called and out is sentinel and env.warned() is None


@pytest.mark.parametrize("task", ["bg_101500_ab12cd", "subagent-0-1a2b3c4d"])
def test_named_task_id_stays_auxiliary(env, task):
    out, called, sentinel = _resume_quiet(env, task_id=task)
    assert called and out is sentinel and env.warned() is None


def test_minted_task_id_with_foreign_request_id_stays_auxiliary(env):
    out, called, sentinel = _resume_quiet(env, api_request_id=f"other:{MINTED}:abcd1234:api:1")
    assert called and out is sentinel and env.warned() is None


@pytest.mark.parametrize("trigger", ["idle", "model_change"])
def test_single_query_block_warns_without_waiting_for_a_prompt(env, monkeypatch, trigger):
    # `chat -Q`, `--oneshot` and `-z` have no screen: the CLI callback would wait out the timeout.
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    env.config.update(cli="block", size_k=1)
    if trigger == "idle":
        seed_session(env, ago=3600)
    else:
        seed_session(env, ago=10, model="p/old")
    monkeypatch.setattr(env.plugin, "_healthy", lambda _name: True)
    env.approval = lambda *a, **k: pytest.fail("single-query must not prompt")
    monkeypatch.setattr(env.plugin, "_gateway_approval", lambda: pytest.fail("single-query must not prompt"))
    out, called, sentinel = run_middleware(env, model="new")
    assert called and out is sentinel and env.warned() is not None


def test_tui_block_without_any_approval_path_falls_back_to_warn(env, monkeypatch):
    env.config.update(cli="block", size_k=1)
    seed_session(env, ago=3600)
    monkeypatch.setattr(env.plugin, "_healthy", lambda _name: True)
    monkeypatch.setattr(env.plugin, "_gateway_approval", lambda: None)
    out, called, sentinel = run_middleware(env, platform="tui")
    assert called and out is sentinel and env.warned() is not None


@pytest.mark.parametrize("trigger", ["idle", "model_change"])
def test_surface_off_cannot_be_reenabled_by_model_change(env, trigger):
    env.config.update(gateway="off", cli="block")
    if trigger == "idle":
        seed_session(env, ago=3600)
    else:
        seed_session(env, ago=10, model="p/old")
    env.approval = lambda *a, **k: pytest.fail("surface off must never prompt")
    out, called, sentinel = run_middleware(env, platform="telegram", model="new")
    assert called and out is sentinel and env.warned() is None


@pytest.mark.parametrize("case", ["short_gap", "small_context", "later_call", "non_cli", "first_call_ever"])
def test_no_trigger(env, case):
    seed_session(env, ago=30 if case == "short_gap" else 3600)
    kw = {}
    if case == "small_context":
        kw["request"] = {"messages": [{"role": "user", "content": "hi"}], "tools": []}
    if case == "later_call":
        kw["api_call_count"] = 2
    if case == "non_cli":
        kw["platform"] = "telegram"
    if case == "first_call_ever":
        kw["session_id"] = "unknown"
    out, called, sentinel = run_middleware(env, **kw)
    assert called and out is sentinel
    assert env.warned() is None


def test_last_real_prompt_tokens_raise_floor(env):
    seed_session(env, ago=3600, prompt_tokens=100000)
    env.config["cli"] = "block"
    prompts = []
    env.approval = lambda command, desc, **kw: (prompts.append(command), "deny")[1]
    run_middleware(env, request={"messages": [], "tools": []})
    assert "100,000" in prompts[0]


# ----------------------------------------------------------- model change --
@pytest.mark.parametrize("mode", ["off", "warn", "block"])
def test_model_change_off_warn_block(env, mode):
    seed_session(env, ago=10, model="p/old")  # short gap: only model-change can fire
    env.config["cli"] = mode
    env.approval = lambda *a, **k: "deny"
    out, called, sentinel = run_middleware(env, model="new")
    if mode == "block":
        assert not called and "message not sent" in out.choices[0].message.content
    else:
        assert called and out is sentinel
        if mode == "warn":
            assert "model changed" in env.warned()
        else:
            assert env.warned() is None


def test_model_change_same_model_silent(env):
    seed_session(env, ago=10, model="p/m1")
    run_middleware(env, model="m1")
    assert env.warned() is None


def test_model_change_small_context_silent(env):
    seed_session(env, ago=10, model="p/old")
    run_middleware(env, model="new", request={"messages": [], "tools": []})
    assert env.warned() is None


# ------------------------------------------------------------------ block --
def _block_setup(env, answer):
    env.config["cli"] = "block"
    seed_session(env, ago=3600)
    calls = []

    def cb(command, desc, **kw):
        calls.append((command, desc, kw))
        if isinstance(answer, Exception):
            raise answer
        return answer

    env.approval = cb
    return calls


def test_block_deny_ends_turn(env):
    calls = _block_setup(env, "deny")
    out, called, _ = run_middleware(env)
    assert not called
    assert "message not sent" in out.choices[0].message.content
    assert out.choices[0].finish_reason == "stop" and out.usage is None
    assert calls[0][2] == {"allow_permanent": False, "allow_session": False}
    assert env.warned() is None  # panel carried the info


@pytest.mark.parametrize("answer", ["once", "session", "always"])
def test_block_other_answers_send(env, answer):
    _block_setup(env, answer)
    out, called, sentinel = run_middleware(env)
    assert called and out is sentinel


def test_block_timeout_denies(env):
    _block_setup(env, "timeout")
    out, called, _ = run_middleware(env)
    assert not called and "message not sent" in out.choices[0].message.content


def test_repeated_denial_still_prompts_for_plugin_tracked_session(env):
    env.config.update(cli="block", size_k=1)
    seed_session(env, ago=7200)
    prompts = []
    env.approval = lambda command, desc, **kw: (prompts.append(command), "deny")[1]
    for _ in range(2):
        out, called, _ = run_middleware(env)
        assert not called and "message not sent" in out.choices[0].message.content
    assert len(prompts) == 2


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


def _crash_after_denied(env, monkeypatch, outcome="cancelled"):
    env.config["cli"] = "block"
    seed_session(env, ago=3600)

    def act(kwargs, is_cli, mode, warn_note, command, deny, model, box=None):
        box["shown"] = True
        box["denied"] = outcome
        raise RuntimeError("after the answer")

    monkeypatch.setattr(env.plugin, "_act", act)


def test_block_callback_raises_sends_and_switches_the_box_off(env):
    _block_setup(env, RuntimeError("boom"))
    out, calls, sentinel = _run_counting(env)
    assert calls == 1 and out is sentinel
    assert env.notices.count(
        "the cacheguard box failed, so messages now go out with a warning "
        "until Hermes restarts. Details: /cacheguard") == 1
    assert env.plugin._H["box_failed"] == "RuntimeError"
    assert "cacheguard box failed this session" in env.plugin._health_text()


def test_after_a_box_crash_later_guarded_messages_warn_and_the_notice_shows_once(env):
    calls = _block_setup(env, RuntimeError("boom"))
    _run_counting(env)
    assert len(calls) == 1
    sends = []
    real_remember = env.plugin._remember_send
    env.plugin._remember_send = lambda *a: (sends.append(1), real_remember(*a))
    for i in range(2):
        sid = f"later{i}"  # a fresh chat each time: the first call already refreshed s1's clock
        seed_session(env, sid, ago=3600)
        out, n, sentinel = _run_counting(env, session_id=sid)
        assert n == 1 and out is sentinel
        assert len(sends) == i + 1  # guarded again (the warn path), not skipped
    assert len(calls) == 1  # the box was never asked again
    assert len(_box_broke_notices(env)) == 1


def test_error_before_the_box_sends_once_and_keeps_the_box(env, monkeypatch):
    _block_setup(env, "deny")
    monkeypatch.setattr(env.plugin, "_evaluate", lambda *a, **k: 1 / 0)
    out, calls, sentinel = _run_counting(env)
    assert calls == 1 and out is sentinel and not _box_broke_notices(env)
    assert not env.plugin._H.get("box_failed")


def test_error_inside_the_screen_before_the_box_sends_once(env, monkeypatch):
    _cli_owner(env, "deny")
    env.config["cli"] = "block"
    seed_session(env, ago=3600)
    monkeypatch.setattr(env.plugin, "_approval_timeout", lambda: 1 / 0)  # runs before the modal is called
    out, calls, sentinel = _run_counting(env)
    assert calls == 1 and out is sentinel and not _box_broke_notices(env)


def test_modal_raises_sends_once_and_switches_the_box_off(env):
    import threading

    class Owner:
        _app = object()
        _approval_lock = threading.Lock()

        def _prompt_text_input_modal(self, **kw):
            raise RuntimeError("painted, then crashed")

        def cb(self, *a, **k):
            raise AssertionError("the modal must be used")

    env.approval = Owner().cb
    env.config["cli"] = "block"
    seed_session(env, ago=3600)
    out, calls, sentinel = _run_counting(env)
    assert calls == 1 and out is sentinel
    assert len(_box_broke_notices(env)) == 1 and env.plugin._H["box_failed"] == "RuntimeError"


def test_error_after_the_box_in_the_main_handler_sends_once(env, monkeypatch):
    env.config["cli"] = "block"
    seed_session(env, ago=3600)

    def act(kwargs, is_cli, mode, warn_note, command, deny, model, box=None):
        box["shown"] = True
        raise RuntimeError("after the box")

    monkeypatch.setattr(env.plugin, "_act", act)
    out, calls, sentinel = _run_counting(env)
    assert calls == 1 and out is sentinel and len(_box_broke_notices(env)) == 1


@pytest.mark.parametrize("outcome", ["cancelled", "timeout"])
def test_error_after_a_known_no_send_answer_stays_not_sent(env, monkeypatch, outcome):
    _crash_after_denied(env, monkeypatch, outcome)
    out, calls, _ = _run_counting(env)
    assert calls == 0 and "message not sent" in out.choices[0].message.content
    assert not _box_broke_notices(env)


def test_error_after_a_known_cancel_and_in_the_not_sent_reply_sends_once(env, monkeypatch):
    _crash_after_denied(env, monkeypatch)
    monkeypatch.setattr(env.plugin, "_denied_response", lambda *a: None)  # building the reply failed
    out, calls, sentinel = _run_counting(env)
    assert calls == 1 and out is sentinel  # never raise into Hermes


def test_cancel_with_a_broken_deny_text_is_still_not_sent_in_plain_words(env):
    _block_setup(env, "deny")

    def broken_deny(outcome, timeout):
        raise RuntimeError("no text")

    handled, response = env.plugin._confirmation_screen({}, "title\n\ndetail", broken_deny, "m")
    assert handled and response.choices[0].message.content == env.plugin._NOT_SENT_TEXT


def test_cancel_then_crash_building_the_reply_in_the_screen_stays_not_sent(env, monkeypatch):
    _block_setup(env, "deny")
    real = env.plugin._SyntheticResponse
    state = {"n": 0}

    def flaky(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("first build fails")
        return real(*a, **k)

    monkeypatch.setattr(env.plugin, "_SyntheticResponse", flaky)
    handled, response = env.plugin._confirmation_screen({}, "title\n\ndetail", None, "m")
    assert handled and response is None  # the first build failed, and so did the retry path: caller sends


def _gateway_block(env, monkeypatch, decision):
    wait = types.ModuleType("tools.approval_gateway_wait")

    def await_decision(*a):
        if isinstance(decision, Exception):
            raise decision
        return decision

    wait._await_gateway_decision = await_decision
    monkeypatch.setitem(sys.modules, "tools.approval_gateway_wait", wait)
    monkeypatch.setattr(env.plugin, "_gateway_approval", lambda: ("key", lambda *a: None))
    env.config.update(cli="off", gateway="block", size_k=1)
    seed_session(env, ago=3600)


@pytest.mark.parametrize("decision", [
    None, "cancel", "x", ["resolved"], {}, {"foo": 1}, RuntimeError("wait crashed"),
    {"resolved": True, "choice": "new-value"},
    {"resolved": None, "choice": None},
    {"resolved": "yes", "choice": "once"},
    {"resolved": False, "choice": "once"},
    {"resolved": False, "choice": "deny"},
])
def test_gateway_bad_decision_or_error_sends_once_and_switches_the_box_off(env, monkeypatch, decision):
    _gateway_block(env, monkeypatch, decision)
    out, calls, sentinel = _run_counting(env, platform="telegram")
    assert calls == 1 and out is sentinel
    assert len(_box_broke_notices(env)) == 1


@pytest.mark.parametrize("decision,word", [
    ({"resolved": True, "choice": "deny"}, "cancelled"),
    ({"resolved": False, "choice": None}, "timeout"),
    ({"resolved": False, "choice": None, "reason": "timeout"}, "timeout"),
    ({"resolved": False, "choice": None, "notify_failed": True}, "could not be delivered"),
    ({"notify_failed": True}, "could not be delivered"),
])
def test_gateway_cancel_timeout_undelivered_stay_not_sent(env, monkeypatch, decision, word):
    _gateway_block(env, monkeypatch, decision)
    out, calls, _ = _run_counting(env, platform="telegram")
    text = out.choices[0].message.content
    assert calls == 0 and "message not sent" in text and word in text
    assert not _box_broke_notices(env) and not env.plugin._H.get("box_failed")


def test_gateway_good_decision_sends_once(env, monkeypatch):
    _gateway_block(env, monkeypatch, {"resolved": True, "choice": "once"})
    out, calls, sentinel = _run_counting(env, platform="telegram")
    assert calls == 1 and out is sentinel and not _box_broke_notices(env)


def test_tui_gateway_retry_after_screen_not_shown_fails_sends_once(env, monkeypatch):
    # No CLI callback (TUI): the screen reports "not shown", then the gateway box is asked and crashes.
    _gateway_block(env, monkeypatch, RuntimeError("wait crashed"))
    env.config.update(cli="block", gateway="off")
    out, calls, sentinel = _run_counting(env, platform="tui")
    assert calls == 1 and out is sentinel and len(_box_broke_notices(env)) == 1


def test_lock_acquire_fails_before_the_box_sends_once(env):
    class Lock:
        def __enter__(self):
            raise RuntimeError("lock broken")

        def __exit__(self, *a):
            return False

    class Owner:
        _app = object()
        _approval_lock = Lock()

        def _prompt_text_input_modal(self, **kw):
            raise AssertionError("never reached")

        def cb(self, *a, **k):
            raise AssertionError("never reached")

    env.approval = Owner().cb
    env.config["cli"] = "block"
    seed_session(env, ago=3600)
    out, calls, sentinel = _run_counting(env)
    assert calls == 1 and out is sentinel and not _box_broke_notices(env)


def test_deny_text_has_no_box_failed_wording(env):
    text = env.plugin._deny_text("s1", gap_s=1800, outcome="cancelled")
    assert "box failed" not in text and "(cancelled)" in text


def test_gateway_import_error_before_the_box_sends_once(env, monkeypatch):
    _gateway_block(env, monkeypatch, {})
    monkeypatch.setitem(sys.modules, "tools.approval_gateway_wait", None)  # import fails
    out, calls, sentinel = _run_counting(env, platform="telegram")
    assert calls == 1 and out is sentinel and not _box_broke_notices(env)


def test_block_without_callback_falls_back_to_warn(env):
    _block_setup(env, "deny")
    env.approval = None
    out, called, sentinel = run_middleware(env)
    assert called and out is sentinel and env.warned() is not None
    assert env.plugin._PENDING["s1"].startswith("CGUARD ")


def test_block_non_cli_never_prompts(env):
    calls = _block_setup(env, "deny")
    out, called, _ = run_middleware(env, platform="discord")
    assert called and not calls


def test_missing_next_call_returns_request(env):
    req = big_request()
    assert env.plugin._on_llm_execution(request=req) is req


# ------------------------------------------------- own session file only --
def test_unknown_session_means_no_box_even_when_hermes_has_history(env):
    con = env.db()
    con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES ('s1','assistant','a',?)",
                (time.time() - 7200,))
    con.execute("INSERT INTO session_model_usage VALUES ('s1','','deepinfra','old',1.0)")
    con.commit()
    con.close()
    env.config.update(cli="block", size_k=1)
    env.approval = lambda *a, **k: pytest.fail("no session file: no box")
    out, called, sentinel = run_middleware(env, model="new", provider="p")
    assert called and out is sentinel and env.warned() is None


@pytest.mark.parametrize("content", ["not json", "[1, 2]", ""])
def test_broken_session_file_means_no_box(env, content):
    env.config.update(cli="block", size_k=1)
    env.plugin._session_file("s1").write_text(content)
    env.approval = lambda *a, **k: pytest.fail("broken session file: no box")
    out, called, sentinel = run_middleware(env, model="new", provider="p")
    assert called and out is sentinel and env.warned() is None


# ------------------------------------------------------ observation hook ---
def _usage(prompt, cached):
    return {"prompt_tokens": prompt, "cache_read_tokens": cached}

def _send_observed(env, **kwargs):
    from types import SimpleNamespace
    response = SimpleNamespace(usage=kwargs.pop("usage", None))
    kwargs.update(next_call=lambda: response, request={}, api_call_count=2, api_mode="chat_completions")
    return env.plugin._on_llm_execution(**kwargs)


def test_load_observations_skips_garbage(env):
    env.plugin._obs_path().write_text("not json\n\n" + json.dumps(_journal_row("s", ts=1.0)) + "\n")
    assert len(env.plugin._load_observations()) == 1


def test_load_observations_skips_malformed_rows_and_keeps_valid_history(env):
    valid = _journal_row("s", ts=1.0)
    env.plugin._obs_path().write_text('[]\n{"model":"bad","gap_s":"nan"}\n' + json.dumps(valid) + "\n")
    assert env.plugin._load_observations() == [valid]


def test_load_observations_skips_unbounded_integer_without_losing_valid_row(env):
    huge = "9" * 1000
    valid = _journal_row("s", ts=1.0)
    env.plugin._obs_path().write_text('{"model":"bad","gap_s":' + huge + '}\n' + json.dumps(valid) + "\n")
    assert env.plugin._load_observations() == [valid]


def test_load_observations_skips_explicit_null_timestamp(env):
    valid = _journal_row("s", ts=1.0)
    env.plugin._obs_path().write_text(
        json.dumps(_journal_row("s", ts=None)) + "\n" + json.dumps(valid) + "\n")
    assert env.plugin._load_observations() == [valid]


def test_read_observations_does_not_reuse_another_files_rows(env, tmp_path, monkeypatch):
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    first.write_text(json.dumps(_journal_row("a", ts=1.0)) + "\n")
    second.write_text(json.dumps(_journal_row("b", ts=1.0)) + "\n")
    os.utime(second, ns=(first.stat().st_atime_ns, first.stat().st_mtime_ns))
    monkeypatch.setattr(env.plugin, "_obs_path", lambda: first)
    assert env.plugin._read_observations()[0]["session_id"] == "a"
    monkeypatch.setattr(env.plugin, "_obs_path", lambda: second)
    assert env.plugin._read_observations()[0]["session_id"] == "b"


def test_rows_without_the_current_fields_are_skipped(env):
    valid = _journal_row("s", ts=1.0)
    old = {"model": "p/m", "gap_s": 1, "cold": True}
    env.plugin._obs_path().write_text(json.dumps(old) + "\n" + json.dumps(valid) + "\n")
    assert env.plugin._load_observations() == [valid]
    assert "invalid observation rows skipped" in env.plugin._H["data_errors"]


def test_malformed_observation_cannot_bypass_block_confirmation(env):
    env.config.update(cli="block", size_k=1)
    env.plugin._obs_path().write_text("[]\n" + json.dumps(_journal_row("x", ts=1.0)) + "\n")
    seed_session(env, session_id="old", ago=7200)
    prompts = []
    env.approval = lambda command, desc, **kw: (prompts.append(command), "deny")[1]
    out, called, _ = run_middleware(env, session_id="old")
    assert not called and "message not sent" in out.choices[0].message.content and len(prompts) == 1


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


def _surface(monkeypatch, plugin, **env):
    values = {"HERMES_SESSION_PLATFORM": "", "HERMES_SESSION_SOURCE": ""}
    values.update(env)
    monkeypatch.delenv("HERMES_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.setattr(plugin, "_session_env", lambda name: values.get(name, ""))
    if "HERMES_INTERACTIVE" in values:
        monkeypatch.setenv("HERMES_INTERACTIVE", values["HERMES_INTERACTIVE"])


@pytest.mark.parametrize("surface", ["telegram", "discord", "slack", "webui"])
def test_table_is_fenced_on_markdown_surfaces(env, monkeypatch, surface):
    _cold_history(env.plugin, cold=2, warm=3, gap=400)
    _surface(monkeypatch, env.plugin, HERMES_SESSION_PLATFORM=surface)
    lines = env.plugin._cmd_show().splitlines()
    start = lines.index("```")
    assert lines[start + 1].startswith("model") and lines[start + 2].startswith("p/m1")
    assert lines[start + 3] == "```" and lines.count("```") == 2
    assert lines.index("Cache lost, by pause length") < start


@pytest.mark.parametrize("env_vars", [
    {},                                               # classic CLI: nothing bound
    {"HERMES_INTERACTIVE": "1"},                      # classic CLI with the terminal flag
    {"HERMES_SESSION_SOURCE": "tui"},
    {"HERMES_SESSION_SOURCE": "desktop"},
    {"HERMES_SESSION_PLATFORM": "cli"},
    {"HERMES_SESSION_PLATFORM": "sms"},               # unknown renderer: stay plain
])
def test_table_stays_plain_on_raw_surfaces(env, monkeypatch, env_vars):
    _cold_history(env.plugin, cold=2, warm=3, gap=400)
    _surface(monkeypatch, env.plugin, **env_vars)
    out = env.plugin._cmd_show()
    assert "```" not in out and len(_table_lines(out)) == 2


def test_webui_without_a_bound_session_is_a_markdown_surface(env, monkeypatch):
    import types
    _cold_history(env.plugin, cold=2, warm=3, gap=400)
    _surface(monkeypatch, env.plugin)
    fake = types.SimpleNamespace(execute_plugin_command=lambda command: "")
    monkeypatch.setitem(sys.modules, "api.commands", fake)
    assert "```" in env.plugin._cmd_show()
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")  # a terminal process is never the web UI
    assert "```" not in env.plugin._cmd_show()


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
    assert "Measurements and session tracking cleared. Settings and exported chats kept." in p._cmd_show("reset")
    assert p._load_observations() == [] and p._get_session("s1") == {}


def test_register(env):
    reg = {"mw": [], "hook": [], "cmd": []}

    class Ctx:
        def register_middleware(self, name, fn):
            reg["mw"].append(name)

        def register_hook(self, name, fn):
            reg["hook"].append(name)

        def register_command(self, name, fn, **kw):
            reg["cmd"].append(name)

    env.plugin.register(Ctx())
    assert reg == {"mw": ["llm_execution", "llm_request"], "hook": ["transform_llm_output"], "cmd": ["cacheguard"]}


def test_register_without_llm_execution_middleware_still_loads(env):
    reg = {"hook": [], "cmd": []}

    class Ctx:
        def register_middleware(self, name, fn):
            if name == "llm_execution":
                raise AttributeError("renamed")

        def register_hook(self, name, fn):
            reg["hook"].append(name)

        def register_command(self, name, fn, **kw):
            reg["cmd"].append(name)

    env.plugin.register(Ctx())
    assert reg == {"hook": ["transform_llm_output"], "cmd": ["cacheguard"]}
    lines = [t for t in env.notices if t == "cacheguard is off: it could not start."]
    assert len(lines) == 1
    assert "llm_execution" not in lines[0] and "AttributeError" not in lines[0]
    assert "AttributeError: renamed" in env.plugin._health_text()  # the detail stays in /cacheguard


@pytest.mark.parametrize("hook, effect", [
    ("llm_request", "cacheguard lines may reach the model."),
    ("transform_llm_output", "no result lines."),
])
def test_health_names_a_hook_that_failed_to_register(env, hook, effect):
    class Ctx:
        def register_middleware(self, name, fn):
            if name == hook:
                raise RuntimeError("refused")

        def register_hook(self, name, fn):
            if name == hook:
                raise RuntimeError("refused")

        def register_command(self, name, fn, **kw):
            pass

    env.plugin.register(Ctx())  # never raises
    text = env.plugin._health_text()
    assert f"{hook}: RuntimeError: refused ({effect})" in text
    assert "health: degraded" in text


def test_wrong_type_in_guard_sends_is_ignored(env):
    env.config["cli"] = "warn"
    put_session(env.plugin, "s1", {"last_api_ts": time.time() - 3600, "model": "p/m1",
                                   "guard_sends": ["not", "a", "dict"]})
    out, called, sentinel = run_middleware(env)
    assert called and out is sentinel and env.warned() is not None
    env.plugin._remember_send({"session_id": "s1", "provider": "p", "model": "m1"}, time.time())
    assert isinstance(env.plugin._get_session("s1")["guard_sends"], dict)


def test_send_time_in_the_future_does_not_hold_the_cooldown(env):
    payload = {"session_id": "s1", "provider": "p", "model": "m1"}
    cfg = {"pause_m": 5}
    now = time.time()
    rec = {"guard_sends": {env.plugin._domain_key(payload): now + 86400}}  # clock went back
    assert not env.plugin._cooldown_active(rec, payload, cfg, now)
    rec = {"guard_sends": {env.plugin._domain_key(payload): now - 60}}
    assert env.plugin._cooldown_active(rec, payload, cfg, now)


def test_last_time_in_the_future_means_no_box(env):
    env.config["cli"] = "block"
    seed_session(env, ago=-86400)  # clock went back: stored time is a day ahead, the pause is unknown
    env.approval = lambda *a, **k: pytest.fail("pause unknown: no box")
    out, called, sentinel = run_middleware(env)
    assert called and out is sentinel


# ------------------------------------------------------ /cacheguard set -----
class _FakeCtx:
    def __init__(self, env, fail=False):
        self.env, self.fail, self.calls = env, fail, []

    def set_config(self, key, value):
        if self.fail:
            raise PermissionError("managed install")
        self.calls.append((key, value))
        self.env.settings[key] = value


def _with_ctx(env, **kw):
    ctx = _FakeCtx(env, **kw)
    env.plugin._CTX = ctx
    return ctx


@pytest.mark.parametrize("args,expected", [
    ("cli block", ("cli", "block")),
    ("gateway warn", ("gateway", "warn")),
    ("desktop block", ("desktop", "block")),
    ("pause_m 10", ("pause_m", 10)),
    ("pause_m 1", ("pause_m", 1)),
    ("size_k 30", ("size_k", 30)),
])
def test_set_writes_and_takes_effect(env, args, expected):
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show("set " + args)
    assert "saved" in out and ctx.calls == [expected]
    assert env.plugin._cfg()[expected[0]] == expected[1]


@pytest.mark.parametrize("args", ["", "cli", "cli maybe", "bot warn", "gateway maybe", "pause_m abc", "pause_m -3", "size_k", "bogus 1", "minutes 10", "minutes 1", "pause_minutes 10", "tokens 30", "cli block extra"])
def test_set_rejects_bad_input_without_writing(env, args):
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show(("set " + args).strip())
    assert "usage:" in out and ctx.calls == []


def test_set_minutes_is_no_longer_a_name(env):
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show("set minutes 5")
    assert out == env.plugin._SET_USAGE and ctx.calls == []


def test_setup_writes_pause_m_key(env):
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show("setup all block 4 20")
    assert ("pause_m", 4) in ctx.calls and "pause ≥ 4 min" in out
    assert env.plugin._cfg()["pause_m"] == 4


def test_old_pause_minutes_config_key_is_ignored(env):
    env.settings = {"pause_minutes": 3}
    assert env.plugin._cfg()["pause_m"] == 10


def test_pause_m_invalid_in_config_falls_back_to_default(env):
    env.settings = {"pause_m": 0.5}
    assert env.plugin._cfg()["pause_m"] == 10
    assert "invalid pause_m" in env.plugin._health_text()


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


def test_setup_rejects_nonfinite_values_before_any_write(env):
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show("setup all block nan 30")
    assert "pause_m must be a number >= 1" in out and ctx.calls == []


def test_invalid_config_uses_visible_defaults_and_keeps_block_mode(env):
    env.config = {
        "cli": "block", "pause_m": "oops",
    }
    cfg = env.plugin._cfg()
    assert cfg["cli"] == "block" and cfg["pause_m"] == 10
    assert "health: degraded" in env.plugin._health_text()


def test_removed_settings_in_old_config_are_ignored_silently(env):
    env.config = {"cli": "block", "notify": "bogus", "gaps_seconds": [300, 60]}
    cfg = env.plugin._cfg()
    assert cfg["cli"] == "block" and "notify" not in cfg and "gaps_seconds" not in cfg
    assert "health: degraded" not in env.plugin._health_text()


def test_malformed_config_section_is_reported(env, monkeypatch):
    import sys

    monkeypatch.setattr(sys.modules["hermes_cli.config"], "read_raw_config",
                        lambda: {"plugins": {"entries": {"cacheguard": {"settings": []}}}})
    cfg = env.plugin._cfg()
    assert cfg["cli"] == "warn"
    assert "health: degraded" in env.plugin._health_text()


def test_config_degraded_status_survives_health_refresh(env, monkeypatch):
    import sys

    env.config["pause_m"] = "bad"
    env.plugin._cfg()
    monkeypatch.setattr(env.plugin, "_health", lambda: {
        name: (True, "") for name in ("approval_cb", "config_api", "state_db", "hermes_version")
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


def test_absent_state_db_is_not_degraded_on_first_run(env):
    assert not (env.home / "state.db").exists()
    assert env.plugin._health()["state_db"] == (True, "")
    env.plugin._run_health()
    assert "state_db" not in env.plugin._health_text()


def test_absent_state_db_features_fall_back_quietly(env):
    assert not (env.home / "state.db").exists()
    env.plugin._run_health()
    assert env.plugin._is_bot_chat("s1") is False


def test_existing_state_db_with_broken_schema_is_still_degraded(env):
    import sqlite3

    con = sqlite3.connect(env.home / "state.db")
    con.executescript("CREATE TABLE messages (id INTEGER PRIMARY KEY);")
    con.commit()
    con.close()
    ok, detail = env.plugin._health()["state_db"]
    assert not ok and "messages" in detail
    env.plugin._run_health()
    assert "state_db" in env.plugin._health_text()


def test_unbounded_config_number_falls_back_without_raising(env):
    env.config["pause_m"] = 10 ** 1000
    cfg = env.plugin._cfg()
    assert cfg["pause_m"] == 10
    assert "health: degraded" in env.plugin._health_text()


def test_set_write_failure_is_reported_not_raised(env):
    _with_ctx(env, fail=True)
    assert "could not write" in env.plugin._cmd_show("set cli block")


def _fake_settings_writer(monkeypatch):
    """Stand-in for hermes_cli.plugins_state.save_plugin_setting; records (plugin_id, segments, value)."""
    saved = []
    mod = types.ModuleType("hermes_cli.plugins_state")
    mod.save_plugin_setting = lambda pid, seg, value: saved.append((pid, seg, value))
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins_state", mod)
    return saved


def test_set_without_ctx_writes_config(env, monkeypatch):
    saved = _fake_settings_writer(monkeypatch)
    env.plugin._CTX = None
    out = env.plugin._cmd_show("set cli block")
    assert "saved" in out and saved == [("cacheguard", ("cli",), "block")]


# ------------------------------------------------ terminal: hermes cacheguard ---
def _cli_args(**kw):
    base = {"cache_guard_action": None, "group": None, "mode": None, "minutes": None,
            "tokens": None}
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_cli_setup_all_block_writes_every_group_and_timing(env, monkeypatch, capsys):
    saved = _fake_settings_writer(monkeypatch)
    env.plugin._CTX = None
    rc = env.plugin._cli_handler(_cli_args(cache_guard_action="setup", group="all", mode="block",
                                            minutes="10", tokens="20"))
    assert rc == 0
    assert ("cacheguard", ("cli",), "block") in saved
    assert ("cacheguard", ("desktop",), "block") in saved
    assert ("cacheguard", ("gateway",), "block") in saved
    assert ("cacheguard", ("pause_m",), 10) in saved
    assert ("cacheguard", ("size_k",), 20) in saved
    assert "saved to config" in capsys.readouterr().out


def test_cli_setup_one_group_without_timing(env, monkeypatch, capsys):
    saved = _fake_settings_writer(monkeypatch)
    env.plugin._CTX = None
    rc = env.plugin._cli_handler(_cli_args(cache_guard_action="setup", group="gateway", mode="warn"))
    assert rc == 0 and saved == [("cacheguard", ("gateway",), "warn")]
    assert "gateway = warn" in capsys.readouterr().out


@pytest.mark.parametrize("kw", [
    dict(group="bogus", mode="block"),
    dict(group="all", mode="maybe"),
    dict(group="all", mode="block", minutes="10"),              # minutes without tokens
    dict(group="all", mode="block", tokens="20"),               # tokens without minutes
    dict(group="all", mode="block", minutes="nan", tokens="20"),
])
def test_cli_bad_input_prints_usage_and_writes_nothing(env, monkeypatch, capsys, kw):
    saved = _fake_settings_writer(monkeypatch)
    env.plugin._CTX = None
    rc = env.plugin._cli_handler(_cli_args(cache_guard_action="setup", **kw))
    out = capsys.readouterr().out
    assert rc == 1 and saved == []
    assert "usage:" in out or "cacheguard:" in out


def test_cli_without_action_prints_the_summary(env, capsys):
    assert env.plugin._cli_handler(_cli_args()) == 0
    out = capsys.readouterr().out
    assert out.startswith("cacheguard  ·  cli warn  ·  desktop warn  ·  gateway off")
    assert "Cache lost, by pause length" in out and "(no observations yet)" in out
    assert "```" not in out
    # Names the terminal command, not the chat command.
    assert "setup: hermes cacheguard setup all block 10 20" in out and "help: hermes cacheguard help" in out
    assert "set: hermes cacheguard set pause_m 1" in out
    assert "/cacheguard" not in out


def test_cli_set_writes_one_setting(env, monkeypatch, capsys):
    saved = _fake_settings_writer(monkeypatch)
    env.plugin._CTX = None
    assert env.plugin._cli_handler(_cli_args(cache_guard_action="set", words=["pause_m", "4"])) == 0
    assert saved == [("cacheguard", ("pause_m",), 4)]
    assert "pause_m = 4 min (saved to config)" in capsys.readouterr().out


@pytest.mark.parametrize("words", [[], ["cli"], ["minutes", "4"], ["pause_m", "0"], ["cli", "maybe"]])
def test_cli_set_bad_input_prints_usage_and_writes_nothing(env, monkeypatch, capsys, words):
    saved = _fake_settings_writer(monkeypatch)
    env.plugin._CTX = None
    assert env.plugin._cli_handler(_cli_args(cache_guard_action="set", words=words)) == 1
    out = capsys.readouterr().out
    assert "usage: hermes cacheguard set <name> <value>" in out and "/cacheguard" not in out
    assert saved == []


def test_cli_help_prints_the_chat_help_text(env, capsys):
    assert env.plugin._cli_handler(_cli_args(cache_guard_action="help")) == 0
    out = capsys.readouterr().out
    assert out.startswith(env.plugin._HELP_TEXT)
    assert "hermes cacheguard` takes: (nothing) | help | setup | set" in out


def test_cli_parser_accepts_nothing_help_set_and_setup(env):
    import argparse
    parser = argparse.ArgumentParser()
    env.plugin._cli_setup_parser(parser)
    assert parser.parse_args([]).cache_guard_action is None
    assert parser.parse_args(["help"]).cache_guard_action == "help"
    assert parser.parse_args(["set", "pause_m", "4"]).words == ["pause_m", "4"]
    with pytest.raises(SystemExit):
        parser.parse_args(["off"])
    args = parser.parse_args(["setup", "all", "block", "10", "20"])
    assert (args.cache_guard_action, args.minutes, args.tokens) == ("setup", "10", "20")


def test_register_adds_cli_command_and_slash_command(env):
    seen = {}

    class Ctx:
        def register_middleware(self, name, fn):
            pass

        def register_hook(self, name, fn):
            pass

        def register_command(self, name, fn, **kw):
            seen["cmd"] = name

        def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
            seen["cli"] = (name, setup_fn, handler_fn)

    env.plugin.register(Ctx())
    assert seen["cmd"] == "cacheguard"
    assert seen["cli"] == ("cacheguard", env.plugin._cli_setup_parser, env.plugin._cli_handler)


def test_register_survives_cli_registration_error(env):
    seen = []

    class Ctx:
        def register_middleware(self, name, fn):
            pass

        def register_hook(self, name, fn):
            pass

        def register_command(self, name, fn, **kw):
            seen.append(name)

        def register_cli_command(self, *a, **kw):
            raise RuntimeError("boom")

    env.plugin.register(Ctx())
    assert seen == ["cacheguard"]


def test_register_without_cli_api_still_loads(env):
    seen = []

    class OldCtx:
        def register_middleware(self, name, fn):
            pass

        def register_hook(self, name, fn):
            pass

        def register_command(self, name, fn, **kw):
            seen.append(name)

    env.plugin.register(OldCtx())
    assert seen == ["cacheguard"]


def test_summary_shows_health_only_when_degraded(env):
    assert "health:" not in env.plugin._cmd_show()
    env.plugin._H["checks"]["state_db"] = (False, "missing or changed")
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


@pytest.mark.parametrize("word", ["?", "clear"])
def test_no_question_mark_or_clear_alias(env, word):
    assert env.plugin._cmd_show(word) != env.plugin._HELP_TEXT
    assert word not in env.plugin._HELP_TEXT.replace("/cacheguard <model>", "")


def test_help_lists_every_command(env):
    text = env.plugin._HELP_TEXT
    for line in ("/cacheguard all","/cacheguard <model>", "/cacheguard setup",
                 "/cacheguard set <name>", "/cacheguard export <first> <last>",
                 "/cacheguard export all", "/cacheguard debug", "/cacheguard reset",
                 "/cacheguard help"):
        assert line in text


def test_defaults_keys_are_frozen(env):
    # A new setting needs the maintainer's explicit OK (AGENTS.md): change this list only when asked.
    assert sorted(env.plugin.DEFAULTS) == sorted([
        "cli", "desktop", "gateway", "display_on_success", "size_k", "pause_m"])


def test_cacheguard_subcommands_are_frozen(env):
    # A new command or alias needs the maintainer's explicit OK (AGENTS.md): change this list only when asked.
    src = inspect.getsource(env.plugin._cmd_show)
    assert set(re.findall(r'== "(\w+)"', src)) == {"help", "debug", "set", "setup", "export", "reset", "all"}
    assert '"?"' not in src and '"clear"' not in src


def test_help_covers_every_subcommand_handled_by_show(env):
    # Any new `/cacheguard <word>` branch in _cmd_show must be documented in help.
    src = inspect.getsource(env.plugin._cmd_show)
    words = set(re.findall(r'== "(\w+)"', src)) | set(re.findall(r'"(reset)"', src))
    assert {"debug", "set", "setup", "export", "reset", "all"} <= words
    for word in words:
        assert f"/cacheguard {word}" in env.plugin._HELP_TEXT


def test_help_lists_every_set_name(env):
    samples = {"cli": "block", "desktop": "block", "display_on_success": "on", "gateway": "block",
               "pause_m": "5", "size_k": "30"}
    for name, raw in samples.items():
        assert env.plugin._parse_setting(name, raw)[0] is not None
        assert f"  {name} " in env.plugin._HELP_TEXT


def test_help_reuses_set_usage(env):
    for line in env.plugin._SET_USAGE.splitlines()[1:]:
        assert line in env.plugin._HELP_TEXT
    assert env.plugin._HELP_TEXT.count("  cli           off | warn | block") == 1


def test_help_has_examples(env):
    text = env.plugin._HELP_TEXT
    assert "Examples" in text
    assert "/cacheguard set gateway block" in text
    assert "  /cacheguard set pause_m 1            change only the pause, here 1 minute" in text
    assert "bot" not in text.lower()


def test_help_says_pause_and_size_apply_to_all_once(env):
    assert env.plugin._HELP_TEXT.lower().count("apply to all") == 1


@pytest.mark.parametrize("args", ["pause_m -1", "pause_m 0", "pause_m 0.5"])
def test_minutes_below_one_gets_the_help_wording(env, args):
    out = env.plugin._cmd_show("set " + args)
    assert out.startswith("cacheguard: pause_m must be a number >= 1.")
    assert "number >= 1" in env.plugin._HELP_TEXT


def test_health_notices_show_no_internal_names(env):
    internal = ("approval_cb", "write_origin", "state.db", "state_db", "llm_execution", "degraded",
                "gateway_approval", "session_key", "Error", "_")
    for name, effect in env.plugin._HEALTH_EFFECTS.items():
        assert effect.endswith(".") and not any(word in effect for word in internal), name


def test_register_args_hint_mentions_help(env):
    seen = {}

    class Ctx:
        def register_middleware(self, name, fn):
            pass

        def register_hook(self, name, fn):
            pass

        def register_command(self, name, fn, **kw):
            seen[name] = kw

    env.plugin.register(Ctx())
    assert "help" in seen["cacheguard"]["args_hint"]


def test_setup_all_sets_all_three_groups(env):
    env.settings = {"cli": "warn", "gateway": "warn"}
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show("setup all block 10 30")
    assert ctx.calls == [("cli", "block"), ("desktop", "block"), ("gateway", "block"),
                         ("pause_m", 10), ("size_k", 30)]
    assert out == ("cacheguard: cli = block · desktop = block · gateway = block · pause ≥ 10 min · size ≥ 30k "
                   "(pause and size apply to all groups; saved to config).")


def test_setup_all_sets_gateway_even_when_it_was_off(env):
    ctx = _with_ctx(env)  # defaults: cli warn, gateway off
    out = env.plugin._cmd_show("setup all block 10 30")
    assert ctx.calls == [("cli", "block"), ("desktop", "block"), ("gateway", "block"),
                         ("pause_m", 10), ("size_k", 30)]
    assert out.startswith("cacheguard: cli = block · desktop = block · gateway = block")
    assert "left off" not in out and "every group is off" not in out


def test_setup_gateway_target_turns_off_gateway_on(env):
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show("setup gateway block 10 30")
    assert ctx.calls == [("gateway", "block"), ("pause_m", 10), ("size_k", 30)]
    assert env.plugin._cfg()["gateway"] == "block"
    assert "left off" not in out and out.startswith("cacheguard: gateway = block")


def test_setup_desktop_target_sets_only_desktop(env):
    env.settings = {"cli": "off"}
    ctx = _with_ctx(env)
    env.plugin._cmd_show("setup desktop block 10 30")
    assert ctx.calls == [("desktop", "block"), ("pause_m", 10), ("size_k", 30)]
    cfg = env.plugin._cfg()
    assert cfg["desktop"] == "block" and cfg["cli"] == "off"


def test_setup_old_form_without_target_is_rejected_and_writes_nothing(env):
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show("setup block 10 30")
    assert out.startswith("cacheguard: Unknown group block; use all, cli, desktop or gateway.\nusage:")
    assert ctx.calls == [] and env.settings == {}


@pytest.mark.parametrize("group", ["cli", "desktop", "gateway"])
def test_setup_with_group_sets_only_that_group(env, group):
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show(f"setup {group} warn 5 20")
    assert ctx.calls == [(group, "warn"), ("pause_m", 5), ("size_k", 20)]
    assert out == (f"cacheguard: {group} = warn · pause ≥ 5 min · size ≥ 20k "
                   "(pause and size apply to all groups; saved to config).")


def test_setup_thresholds_are_global_even_with_a_group(env):
    ctx = _with_ctx(env)
    env.plugin._cmd_show("setup gateway off 10 30")
    assert ("pause_m", 10) in ctx.calls and ("size_k", 30) in ctx.calls
    assert not any(k.endswith(("_gap_seconds", "_context_tokens")) and k not in ("min_gap_seconds", "min_context_tokens")
                   for k, _ in ctx.calls)


def test_setup_does_not_depend_on_where_it_is_typed(env, monkeypatch):
    monkeypatch.setattr(env.plugin, "_session_env", lambda name: "telegram")
    env.settings = {"gateway": "warn"}
    ctx = _with_ctx(env)
    env.plugin._cmd_show("setup all block 10 30")
    assert ctx.calls[:3] == [("cli", "block"), ("desktop", "block"), ("gateway", "block")]


def test_set_cli_names_the_key_it_wrote(env):
    _with_ctx(env)
    out = env.plugin._cmd_show("set cli warn")
    assert out == "cacheguard: cli = warn (saved to config)."


def test_setup_accepts_off_mode(env):
    env.settings = {"cli": "warn", "gateway": "warn"}
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show("setup all off 10 30")
    assert "saved" in out and ctx.calls == [
        ("cli", "off"), ("desktop", "off"), ("gateway", "off"), ("pause_m", 10), ("size_k", 30),
    ]
    assert out.count("saved") == 1


@pytest.mark.parametrize("args", ["", "block 10", "block 10 30", "maybe 10 30", "block abc 30", "block 10 -1",
                                  "block warn 10 30 5", "block warn 10 30", "cli block warn 10 30", "cli", "cli block 10",
                                  "cli block 10 30 5", "web block 10 30", "telegram warn 10 30", "gateway maybe 10 30",
                                  "all maybe 10 30", "all warn 10 -1", "all warn abc 30", "all block 10 30 extra",
                                  "desktop warn 10"])
def test_setup_writes_nothing_unless_all_valid(env, args):
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show(("setup " + args).strip())
    assert "usage:" in out and ctx.calls == []


def test_setup_all_off_writes_only_modes(env):
    env.settings = {"cli": "warn", "gateway": "block", "min_gap_seconds": 600}
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show("setup all off")
    assert ctx.calls == [("cli", "off"), ("desktop", "off"), ("gateway", "off")]
    assert out == "cacheguard: cli = off · desktop = off · gateway = off (saved to config)."


def test_setup_mode_only_keeps_pause_and_size(env):
    env.settings = {"pause_m": 10, "size_k": 30}
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show("setup cli block")
    assert ctx.calls == [("cli", "block")]
    assert env.plugin._cfg()["pause_m"] == 10 and env.plugin._cfg()["size_k"] == 30
    assert "pause" not in out and "context" not in out and out.endswith("(saved to config).")


@pytest.mark.parametrize("args", ["all off 10", "cli block 30", "all block 10", "gateway warn 5 20 1"])
def test_setup_one_of_minutes_or_size_is_usage_and_writes_nothing(env, args):
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show("setup " + args)
    if len(args.split()) == 3:
        assert out.startswith("cacheguard: Give both pause and size, or omit both.\nusage: /cacheguard setup")
    else:
        assert out.startswith("usage: /cacheguard setup")
    assert ctx.calls == []


def test_setup_all_off_turns_the_three_groups_off(env):
    env.settings = {"cli": "block", "gateway": "warn"}
    ctx = _with_ctx(env)
    assert "saved" in env.plugin._cmd_show("setup all off")
    assert ctx.calls == [("cli", "off"), ("desktop", "off"), ("gateway", "off")]


def test_reset_with_extra_arguments_is_refused(env, monkeypatch):
    saved = _fake_settings_writer(monkeypatch)
    assert env.plugin._cmd_show("reset now") == "usage: /cacheguard reset"
    assert saved == []


@pytest.mark.parametrize("word", ["off", "on"])
def test_off_and_on_are_not_commands(env, monkeypatch, word):
    saved = _fake_settings_writer(monkeypatch)
    assert env.plugin._cmd_show(word) == f"No recorded model matches {word}. Use /cacheguard all to list recorded models."
    assert saved == []
    assert f"/cacheguard {word}\n" not in env.plugin._HELP_TEXT


def test_enabled_is_not_a_setting(env):
    assert "enabled" not in env.plugin.DEFAULTS
    assert env.plugin._parse_setting("enabled", "off")[0] is None
    env.config["enabled"] = False  # an old key in config.yaml is ignored
    assert "enabled" not in env.plugin._cfg() and env.plugin._health_text() == ""
    seed_session(env, ago=3600)
    out, called, _ = run_middleware(env)
    assert called and env.warned() is not None  # cacheguard still acts


def test_unknown_arguments_are_named(env):
    _seed_rows(env, ["p/model-a"], per=5)
    unknown = "unknown argument '{}'. Try /cacheguard help"
    assert env.plugin._cmd_show("all foo bar") == unknown.format("foo")
    assert env.plugin._cmd_show("foo") == (
        "No recorded model matches foo. Use /cacheguard all to list recorded models.")
    assert env.plugin._cmd_show("model-a foo") == unknown.format("foo")
    assert env.plugin._cmd_show("help me") == unknown.format("me")
    assert env.plugin._cmd_show("debug x") == unknown.format("x")
    assert "p/model-a" in env.plugin._cmd_show("MODEL-A")  # a recorded model still filters
    assert "p/model-a" in env.plugin._cmd_show("all")


def test_bad_input_on_every_subcommand_gets_usage(env):
    assert env.plugin._cmd_show("setup").startswith("usage: /cacheguard setup")
    assert env.plugin._cmd_show("setup nope warn").startswith(
        "cacheguard: Unknown group nope; use all, cli, desktop or gateway.")
    assert env.plugin._cmd_show("set").startswith("usage: /cacheguard set")
    assert env.plugin._cmd_show("set cli maybe").startswith("cacheguard: cli must be")
    assert env.plugin._cmd_show("export").startswith("usage: /cacheguard export")
    assert env.plugin._cmd_show("export x y").startswith("usage: /cacheguard export")


def test_visible_name_is_cacheguard(env):
    assert env.plugin._HELP_TEXT.startswith("cacheguard help")
    assert "label" not in env.plugin.DEFAULTS


def test_terminal_usage_names_the_terminal_command(env, monkeypatch, capsys):
    _fake_settings_writer(monkeypatch)
    env.plugin._CTX = None
    rc = env.plugin._cli_handler(_cli_args(cache_guard_action="setup", group="bogus", mode="block"))
    out = capsys.readouterr().out
    assert rc == 1 and "usage: hermes cacheguard setup" in out and "/cacheguard" not in out


# ------------------------------------------- health: private symbols ---
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


# The way Hermes 0.22.2 defines the function the box leans on most.
_WAIT_SIGNATURE = "session_key, notify_cb, approval_data, *, surface='gateway'"
_WAIT_CHANGES = {  # a changed Hermes, and the plain reason we expect to name
    "extra argument": ("def _await_gateway_decision(session_key, notify_cb, approval_data, extra):\n    return {}\n",
                       "it now needs `extra`"),
    "extra argument first": ("def _await_gateway_decision(extra, session_key, notify_cb, approval_data):\n    return {}\n",
                             "its arguments are now (extra, session_key, notify_cb), not (session_key, notify_cb, approval_data)"),
    "renamed argument": ("def _await_gateway_decision(session_key, notify, approval_data):\n    return {}\n",
                         "its arguments are now (session_key, notify, approval_data), not (session_key, notify_cb, approval_data)"),
    "removed argument": ("def _await_gateway_decision(session_key, notify_cb):\n    return {}\n",
                         "it no longer takes `approval_data`"),
    "sync to async": ("async def _await_gateway_decision(session_key, notify_cb, approval_data):\n    return {}\n",
                      "it is now async"),
    "function to class": ("class _await_gateway_decision:\n    pass\n", "it is no longer a function"),
    "function to variable": ("_await_gateway_decision = None\n", "it is no longer a function"),
}
_WAIT_FINE = {  # harmless changes: no error
    "same as Hermes": f"def _await_gateway_decision({_WAIT_SIGNATURE}):\n    return {{}}\n",
    "new optional argument at the end": "def _await_gateway_decision(session_key, notify_cb, approval_data, *, surface='gateway', more=None):\n    return {}\n",
    "new optional positional at the end": "def _await_gateway_decision(session_key, notify_cb, approval_data, more=1):\n    return {}\n",
    "extra arguments swallowed": "def _await_gateway_decision(*args, **kwargs):\n    return {}\n",
}


def _loaded_wait_module(monkeypatch, source):
    import sys

    approval = types.ModuleType("tools.approval")
    approval._gateway_notify_cb = lambda session_key: None
    gate = types.ModuleType("tools.approval_gateway_wait")
    exec(source, gate.__dict__)
    monkeypatch.setitem(sys.modules, "tools.approval", approval)
    monkeypatch.setitem(sys.modules, "tools.approval_gateway_wait", gate)


@pytest.mark.parametrize("change", _WAIT_CHANGES)
def test_health_names_a_changed_call_shape_in_a_loaded_module(env, monkeypatch, change):
    source, reason = _WAIT_CHANGES[change]
    _loaded_wait_module(monkeypatch, source)
    ok, detail = env.plugin._health()["gateway_approval"]
    assert not ok and detail == f"Hermes changed `_await_gateway_decision`: {reason}"
    env.plugin._run_health()
    text = env.plugin._health_text()
    assert "gateway_approval: Hermes changed `_await_gateway_decision`" in text
    assert "cacheguard cannot show the box here: it only warns." in text
    assert not any("_await_gateway" in line for line in env.notices)  # the name stays in /cacheguard


@pytest.mark.parametrize("change", _WAIT_FINE)
def test_health_accepts_harmless_call_shape_changes(env, monkeypatch, change):
    _loaded_wait_module(monkeypatch, _WAIT_FINE[change])
    assert env.plugin._health()["gateway_approval"] == (True, "")


@pytest.mark.parametrize("change", _WAIT_CHANGES)
def test_health_names_a_changed_call_shape_read_from_source(env, tmp_path, monkeypatch, change):
    source, reason = _WAIT_CHANGES[change]
    module = _fake_hermes_module(tmp_path, monkeypatch, source)
    monkeypatch.setitem(env.plugin._CALLS, (module, "_await_gateway_decision"),
                        env.plugin._CALLS[("tools.approval_gateway_wait", "_await_gateway_decision")])
    assert env.plugin._call_problem(module, "_await_gateway_decision") == (
        f"Hermes changed `_await_gateway_decision`: {reason}")
    assert "cgfake_marker" not in sys.modules  # read, never imported


@pytest.mark.parametrize("change", _WAIT_FINE)
def test_harmless_call_shape_changes_read_from_source(env, tmp_path, monkeypatch, change):
    module = _fake_hermes_module(tmp_path, monkeypatch, _WAIT_FINE[change])
    monkeypatch.setitem(env.plugin._CALLS, (module, "_await_gateway_decision"),
                        env.plugin._CALLS[("tools.approval_gateway_wait", "_await_gateway_decision")])
    assert env.plugin._call_problem(module, "_await_gateway_decision") == ""


def test_keyword_argument_we_pass_must_still_exist(env, tmp_path, monkeypatch):
    module = _fake_hermes_module(tmp_path, monkeypatch, "def normalize_usage(response_usage, *, provider=None):\n    pass\n")
    monkeypatch.setitem(env.plugin._CALLS, (module, "normalize_usage"),
                        env.plugin._CALLS[("agent.usage_pricing", "normalize_usage")])
    assert env.plugin._call_problem(module, "normalize_usage") == (
        "Hermes changed `normalize_usage`: it no longer takes `api_mode`")


def test_unreadable_hermes_source_is_reported_not_raised(env, tmp_path, monkeypatch):
    module = _fake_hermes_module(tmp_path, monkeypatch, "def _await_gateway_decision(:\n")
    monkeypatch.setitem(env.plugin._CALLS, (module, "_await_gateway_decision"),
                        env.plugin._CALLS[("tools.approval_gateway_wait", "_await_gateway_decision")])
    assert env.plugin._call_problem(module, "_await_gateway_decision") == (
        "cannot read `_await_gateway_decision`: SyntaxError")


def test_every_checked_call_has_a_signature_to_compare(env):
    # Each function the health check reads must have an expected shape, or a change would pass.
    shaped = {symbol for _module, symbol in env.plugin._CALLS}
    for symbol in ("_get_approval_callback", "_gateway_notify_cb", "_await_gateway_decision",
                   "CLIModalMixin._normalize_slash_confirm_choice", "normalize_usage",
                   "get_current_session_key", "get_current_write_origin", "read_raw_config",
                   "get_hermes_home", "get_session_env", "is_bot_mode_managed",
                   "_is_single_query_approval_context", "_get_approval_timeout"):
        assert symbol in shaped


def test_health_skips_cli_object_symbols(env):
    assert not any("modal" in name or "lock" in name for name in env.plugin._health())


def test_cfg_reads_the_read_only_config_when_available(env, monkeypatch):
    import sys

    calls = []
    mod = sys.modules["hermes_cli.config"]
    monkeypatch.setattr(mod, "read_raw_config_readonly", lambda: (calls.append(1), {"plugins": {"entries": {"cacheguard": {"settings": {"cli": "block"}}}}})[1],
                        raising=False)
    monkeypatch.setattr(mod, "read_raw_config", lambda: pytest.fail("deep-copying read used"))
    assert env.plugin._cfg()["cli"] == "block" and calls


def _seed_chat(env, n, session_id="s1"):
    con = env.db()
    for i in range(n):
        con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                    (session_id, "user" if i % 2 == 0 else "assistant", f"msg{i + 1}", float(i)))
    con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                (session_id, "tool", "toolnoise", 99.0))
    con.commit()
    con.close()


def test_export_head_tail(env):
    _seed_chat(env, 10)
    run_middleware(env)  # records the current session
    _set_export_cli(env, "s1")
    out = env.plugin._cmd_show("export 2 2")
    assert "4 of 10 messages" in out
    path = out.splitlines()[1]
    text = open(path, encoding="utf-8").read()
    assert "msg1" in text and "msg2" in text and "msg9" in text and "msg10" in text
    assert "msg5" not in text and "toolnoise" not in text
    assert "6 messages omitted" in text


@pytest.mark.skipif(os.name == "nt", reason="no file modes on Windows")
def test_export_file_is_owner_only(env):
    _seed_chat(env, 4)
    run_middleware(env)
    _set_export_cli(env, "s1")
    path = env.plugin._cmd_show("export all").splitlines()[1]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # a file left by an older version with open rights is tightened too
    os.chmod(path, 0o644)
    env.plugin._write_private_text(Path(path), "x")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_export_chmod_failure_does_not_break_write(env, tmp_path, monkeypatch):
    def boom(*a):
        raise OSError("no modes here")
    monkeypatch.setattr(os, "fchmod", boom, raising=False)
    target = tmp_path / "a.md"
    env.plugin._write_private_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_export_all_and_overlap(env):
    _seed_chat(env, 6)
    run_middleware(env)
    _set_export_cli(env, "s1")
    for cmd in ("export all", "export 5 5"):
        out = env.plugin._cmd_show(cmd)
        assert "6 of 6 messages" in out
        assert "omitted" not in open(out.splitlines()[1], encoding="utf-8").read()


@pytest.mark.parametrize("args", ["export", "export 3", "export a b", "export 0 0", "export -1 2"])
def test_export_bad_args(env, args):
    assert "usage: /cacheguard export" in env.plugin._cmd_show(args)


def test_export_without_session(env):
    out = env.plugin._cmd_show("export all")
    assert "only in the Hermes CLI" in out and "/new" in out and "session_search" not in out


def test_export_ignores_non_cli(env):
    run_middleware(env, platform="telegram")
    assert "only in the Hermes CLI" in env.plugin._cmd_show("export all")


def test_gateway_export_hint_quotes_previous_session(env, monkeypatch):
    monkeypatch.setattr(env.plugin, "_gateway_session_id", lambda: 'old"id')
    out = env.plugin._cmd_show("export all")
    assert "1. Start fresh: `/new`" in out
    assert 'Read back session "old\\"id" and continue' in out


def _set_export_cli(env, session_id, agent=...):
    from types import SimpleNamespace

    cli = SimpleNamespace(session_id=session_id)
    if agent is not ...:
        cli.agent = agent
    env.plugin._CTX = SimpleNamespace(_manager=SimpleNamespace(_cli_ref=cli))


def test_export_uses_live_cli_session_after_switch(env):
    from types import SimpleNamespace

    _seed_chat(env, 2, "A")
    _seed_chat(env, 2, "B")
    _set_export_cli(env, "B", agent=SimpleNamespace(session_id="A"))
    out = env.plugin._cmd_show("export all")
    assert "2 of 2 messages" in out
    exported = open(out.splitlines()[1], encoding="utf-8").read()
    assert "(B)" in exported and "(A)" not in exported


def test_export_after_new_uses_fresh_cli_session(env):
    from types import SimpleNamespace

    _seed_chat(env, 2, "A")
    _set_export_cli(env, "A", agent=SimpleNamespace(session_id="A"))
    cli = env.plugin._CTX._manager._cli_ref
    cli.session_id = "B"
    cli.agent.session_id = "B"
    out = env.plugin._cmd_show("export all")
    assert "nothing to export" in out
    assert not list((env.home / "cacheguard" / "exports").glob("*.md"))


@pytest.mark.parametrize("last", [5, 10])
def test_export_partial_counts_first_and_last_messages(env, last):
    _seed_chat(env, 30)
    _set_export_cli(env, "s1")
    out = env.plugin._cmd_show(f"export 5 {last}")
    assert f"{5 + last} of 30 messages" in out
    text = open(out.splitlines()[1], encoding="utf-8").read()
    contents = [line for line in text.splitlines() if line.startswith("msg")]
    assert contents == [f"msg{i}" for i in range(1, 6)] + [f"msg{i}" for i in range(31 - last, 31)]


def test_export_unknown_live_session_does_not_fall_back_to_cached(env):
    _seed_chat(env, 2, "A")
    _set_export_cli(env, None, agent=None)
    out = env.plugin._cmd_show("export all")
    assert "only in the Hermes CLI" in out
    assert not list((env.home / "cacheguard" / "exports").glob("*.md"))


def test_export_after_blocked_send_uses_active_cli_session(env):
    _seed_chat(env, 2, "A")
    _seed_chat(env, 2, "B")
    seed_session(env, session_id="B", ago=3600)
    env.config.update(cli="block", size_k=1)
    env.approval = lambda *a, **k: "deny"
    denied, called, _ = run_middleware(env, session_id="B")
    assert not called and "message not sent" in denied.choices[0].message.content
    _set_export_cli(env, "B", agent=None)
    out = env.plugin._cmd_show("export all")
    assert "2 of 2 messages" in out
    exported = open(out.splitlines()[1], encoding="utf-8").read()
    assert "(B)" in exported and "(A)" not in exported and "message not sent" not in exported


# ---------------------------------------- system prompt + outcome report -----
def test_estimate_counts_separate_system_prompt(env):
    p = env.plugin
    base = {"messages": [{"role": "user", "content": "x" * 4000}], "tools": []}
    def est(request):
        return p.score_risk({}, {"request": request})["tokens_at_risk"]

    plain = est(base)
    with_sys = est(dict(base, system="y" * 40000))
    with_instr = est(dict(base, instructions="y" * 40000))
    assert with_sys >= plain + 9000 and with_instr >= plain + 9000


def _guard_acts(env, monkeypatch, cached=59000, platform="cli", first_cached=59000):
    from types import SimpleNamespace
    env.config.setdefault("display_on_success", True)  # these tests read the kept-cache line (default is off)
    now = [100.0]
    monkeypatch.setattr(env.plugin.time, "time", lambda: now[0])
    _send_observed(env, session_id="s1", model="m1", provider="p", usage=_usage(60000, first_cached))
    now[0] = 3700.0
    response = SimpleNamespace(usage=_usage(60000, cached))
    return env.plugin._on_llm_execution(next_call=lambda: response, request=big_request(),
                                       session_id="s1", model="m1", provider="p", api_call_count=1,
                                       api_mode="chat_completions", platform=platform)


def _reply(env, text="Hello", session_id="s1"):
    return env.plugin._transform_llm_output(
        response_text=text, session_id=session_id, model="p/m1", platform="cli", turn_id="t1")


def test_outcome_kept_is_appended_once(env, monkeypatch):
    _guard_acts(env, monkeypatch)
    assert not env.notices
    assert _reply(env) is None and env.posted == [RESULT_LINE]
    assert _reply(env) is None and env.posted == [RESULT_LINE]


def test_outcome_kept_is_hidden_by_default(env, monkeypatch):
    env.config["display_on_success"] = False
    _guard_acts(env, monkeypatch)
    assert env.plugin._PENDING == {} and env.plugin._WARNED == {} and _reply(env) is None


def _turn(env, monkeypatch, *, mode="warn", cached=59000, model="m1", answer="once"):
    """Baseline call, then a call 60 minutes later on the same session. Returns (provider calls, boxes shown)."""
    from types import SimpleNamespace
    env.config["cli"] = mode
    calls, boxes = [], []
    env.approval = lambda command, desc, **kw: (boxes.append(command), answer)[1]
    now = [100.0]
    monkeypatch.setattr(env.plugin.time, "time", lambda: now[0])
    _send_observed(env, session_id="s1", model="m1", provider="p", usage=_usage(60000, 59000))
    now[0] = 3700.0
    response = SimpleNamespace(usage=_usage(60000, cached))
    env.plugin._on_llm_execution(next_call=lambda: (calls.append(1), response)[1], request=big_request(),
                                 session_id="s1", model=model, provider="p", api_call_count=1,
                                 api_mode="chat_completions", platform="cli")
    return calls, boxes


@pytest.mark.parametrize("mode", ["warn", "block"])
def test_kept_line_hidden_when_option_unset(env, monkeypatch, mode):
    assert "display_on_success" not in env.config
    calls, boxes = _turn(env, monkeypatch, mode=mode)
    assert calls == [1] and len(boxes) == (1 if mode == "block" else 0)
    assert env.plugin._PENDING == {} and _reply(env) is None


def test_kept_line_shown_when_option_on_warn(env, monkeypatch):
    env.config["display_on_success"] = True
    _turn(env, monkeypatch, mode="warn")
    assert _reply(env) is None and env.posted == [RESULT_LINE]


def test_kept_line_shown_when_option_on_block(env, monkeypatch):
    env.config["display_on_success"] = True
    calls, boxes = _turn(env, monkeypatch, mode="block")
    assert calls == [1] and len(boxes) == 1
    assert _reply(env) is None and env.posted == ["CGUARD 59k reused, 60 min"]


@pytest.mark.parametrize("mode,tag", [("warn", "WARN "), ("block", "")])
def test_loss_line_shown_when_option_off(env, monkeypatch, mode, tag):
    env.config["display_on_success"] = False
    _turn(env, monkeypatch, mode=mode, cached=1000)
    assert _reply(env) is None
    assert env.posted == [f"CGUARD {tag}cache lost, 59k rebilled, 60 min"]


def test_unknown_line_shown_when_option_off(env, monkeypatch):
    env.config["display_on_success"] = False
    _turn(env, monkeypatch, mode="warn", cached=0)
    assert _reply(env) is None
    assert env.posted == ["CGUARD WARN cache unknown, too few cache hits, 60 min"]


def test_model_changed_line_hidden_when_cache_kept_and_option_off(env, monkeypatch):
    env.config["display_on_success"] = False
    _turn(env, monkeypatch, mode="warn", model="m2")
    assert env.plugin._PENDING == {} and _reply(env) is None


def test_model_changed_line_shown_when_cache_kept_and_option_on(env, monkeypatch):
    env.config["display_on_success"] = True
    _turn(env, monkeypatch, mode="warn", model="m2")
    assert _reply(env) is None and env.posted == ["CGUARD OK 59k reused, model changed"]


def test_model_changed_line_shown_when_cache_lost_whatever_the_option(env, monkeypatch):
    env.config["display_on_success"] = False
    _turn(env, monkeypatch, mode="warn", model="m2", cached=0)
    assert _reply(env) is None
    assert env.posted == ["CGUARD WARN cache lost, 60k rebilled, model changed"]


def test_cache_lost_is_one_rule_for_every_turn(env):
    assert env.plugin._cache_lost(30, 100) and not env.plugin._cache_lost(29, 100)


@pytest.mark.parametrize("bad", ["yes", 1])
def test_bad_display_value_acts_as_false_and_is_reported(env, bad):
    env.config["display_on_success"] = bad
    assert env.plugin._cfg()["display_on_success"] is False
    assert "invalid display_on_success; using default" in env.plugin._H["config_errors"]
    assert "config: invalid display_on_success; using default" in env.plugin._health_text()


def test_set_display_on_success_is_saved(env, monkeypatch):
    ctx = _with_ctx(env)
    assert "saved" in env.plugin._cmd_show("set display_on_success on")
    assert "saved" in env.plugin._cmd_show("set display_on_success off")
    assert ctx.calls == [("display_on_success", True), ("display_on_success", False)]
    assert env.settings["display_on_success"] is False
    env.plugin._CTX = None
    saved = _fake_settings_writer(monkeypatch)
    env.plugin._cmd_show("set display_on_success on")
    assert saved == [("cacheguard", ("display_on_success",), True)]


def test_set_display_on_success_refuses_true(env):
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show("set display_on_success true")
    assert out.startswith("cacheguard: display_on_success must be on or off.")
    assert ctx.calls == []


@pytest.mark.parametrize("display,answer", [(False, "once"), (True, "once"), (False, "deny"), (True, "deny")])
def test_display_option_never_changes_what_is_sent(env, monkeypatch, display, answer):
    env.config["display_on_success"] = display
    calls, boxes = _turn(env, monkeypatch, mode="block", answer=answer)
    assert calls == ([1] if answer == "once" else [])
    assert len(boxes) == 1 and "Paused 60 minutes" in boxes[0]


def test_outcome_stays_for_its_own_turn(env, monkeypatch):
    real = env.plugin._queue_note
    monkeypatch.setattr(env.plugin, "_queue_note",
                        lambda sid, text, turn_id=None: real(sid, text, "t1"))
    _guard_acts(env, monkeypatch)
    other = env.plugin._transform_llm_output(
        response_text="side answer", session_id="s1", model="p/m1", platform="cli", turn_id="t2")
    assert other is None and "s1" in env.plugin._PENDING
    assert _reply(env) is None and env.posted == [RESULT_LINE]


def test_stale_outcome_from_no_text_turn_is_dropped(env, monkeypatch):
    _guard_acts(env, monkeypatch)  # turn ends without final text: line never shown
    assert "s1" in env.plugin._PENDING
    from types import SimpleNamespace
    env.plugin._on_llm_execution(next_call=lambda: SimpleNamespace(usage=_usage(60000, 59000)),
                                 request=big_request(), session_id="s1", model="m1", provider="p",
                                 api_call_count=1, api_mode="chat_completions", platform="cli")
    assert _reply(env) is None


def test_stale_gateway_warning_is_dropped_at_turn_start(env, monkeypatch):
    env.plugin._WARNED["s1"] = "old warning"  # left by a turn whose provider call failed
    _guard_acts(env, monkeypatch)
    _reply(env)
    assert all("old warning" not in line for line in env.posted)
    assert "s1" not in env.plugin._WARNED


def test_outcome_loss_reports_rebilled_tokens_and_pause(env, monkeypatch):
    _guard_acts(env, monkeypatch, cached=1000)
    _reply(env)
    assert env.posted == ["CGUARD WARN cache lost, 59k rebilled, 60 min"]


def _zero_reads(env, count):
    for i in range(count):
        append_observation(env.plugin, {
            "ts": 800 + i, "session_id": f"z{i}", "model": "p/m1", "api_mode": "chat_completions",
            "api_request_id": f"z{i}", "gap_s": 0, "prompt_tokens": 50000,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "prev_prompt_tokens": 50000, "cause": None,
        })


def test_model_without_cache_says_so_once_then_stays_silent(env, monkeypatch):
    env.plugin._NO_CACHE_SAID.clear()
    _zero_reads(env, 3)
    _guard_acts(env, monkeypatch, cached=0, first_cached=0)
    _reply(env)
    assert env.posted == ["CGUARD WARN cache unknown, model reports no cache"]
    assert env.plugin._NO_CACHE_SAID == {"p/m1"}
    row = {"evaluable": False, "cause": None, "cache_read_tokens": 0, "prompt_tokens": 60000,
           "model": "p/m1", "api_mode": "chat_completions", "gap_s": 3600}
    env.plugin._queue_outcome({"session_id": "s2"}, True, row)
    assert env.plugin._PENDING.get("s2") is None  # said once for this model: nothing more
    env.plugin._NO_CACHE_SAID.clear()
    env.plugin._queue_outcome({"session_id": "s2"}, True, row)
    assert "model reports no cache" in env.plugin._PENDING["s2"]


def test_zero_reads_with_too_little_history_do_not_claim_no_cache(env, monkeypatch):
    env.plugin._NO_CACHE_SAID.clear()
    _guard_acts(env, monkeypatch, cached=0, first_cached=0)  # only 1 earlier zero read plus this one
    _reply(env)
    assert all("no cache" not in line for line in env.posted)


def test_model_that_once_read_the_cache_is_not_called_cacheless(env, monkeypatch):
    env.plugin._NO_CACHE_SAID.clear()
    _zero_reads(env, 3)
    _guard_acts(env, monkeypatch, cached=0)  # the first call of this chat read 59,000
    _reply(env)
    assert all("no cache" not in line for line in env.posted)


def test_zero_read_with_one_positive_reports_no_baseline_yet(env, monkeypatch):
    _guard_acts(env, monkeypatch, cached=0)
    _reply(env)
    assert env.posted == ["CGUARD WARN cache unknown, too few cache hits, 60 min"]


def test_outcome_without_finite_pause_omits_clause(env):
    row = {"evaluable": True, "cold": True, "miss_delta": 5, "gap_s": None}
    assert env.plugin._outcome_text(row) == "cache lost, 5 rebilled"


def test_gateway_outcome_uses_the_same_label_as_the_cli(env, monkeypatch):
    env.config.update(gateway="warn")
    _guard_acts(env, monkeypatch, platform="telegram")
    assert _reply(env) is None
    assert env.posted == [RESULT_LINE]


def test_label_key_in_config_is_ignored(env, monkeypatch):
    env.config.update(label="🚧cacheguard🚧", gateway="warn")
    assert "label" not in env.plugin._cfg() and not env.plugin._H["config_errors"]
    _guard_acts(env, monkeypatch)
    _reply(env)
    assert env.posted == [RESULT_LINE]
    assert env.plugin._deny_text("s1", gap_s=60).startswith("[CACHEGUARD] BLOCKED · message not sent")


def test_set_label_is_gone(env):
    assert "label" not in env.plugin._SET_USAGE and "label" not in env.plugin._HELP_TEXT
    assert env.plugin._cmd_show("set label x") == env.plugin._SET_USAGE


def test_outcome_is_per_session(env, monkeypatch):
    _guard_acts(env, monkeypatch)
    _reply(env, session_id="other")
    assert env.posted == []
    _reply(env)
    assert env.posted == [RESULT_LINE]


def test_transform_never_breaks_reply(env, monkeypatch):
    _guard_acts(env, monkeypatch)
    assert env.plugin._transform_llm_output(response_text=None, session_id="s1") is None
    assert env.plugin._transform_llm_output(session_id=["bad"]) is None
    monkeypatch.setattr(env.plugin, "_PENDING", None)
    assert env.plugin._transform_llm_output(response_text="x", session_id="s1") is None


def test_no_outcome_when_guard_did_not_act(env):
    _send_observed(env, session_id="s1", model="m1", provider="p", usage=_usage(60000, 59000))
    assert env.warned() is None and _reply(env) is None


def test_no_outcome_when_user_denied(env):
    env.config = {"cli": "block", "min_gap_seconds": 0, "size_k": 1}
    env.approval = lambda *a, **k: "deny"
    seed_session(env, ago=3600, prompt_tokens=60000)
    result, called, _ = run_middleware(env, request=big_request())
    assert not called and "message not sent" in result.choices[0].message.content
    assert env.warned() is None and env.plugin._load_observations() == []
    assert _reply(env) is None


def test_gateway_deny_text_omits_cli_only_export(env):
    text = env.plugin._deny_text("s1", gateway=True)
    assert "/cacheguard export" not in text
    assert "/new" in text and "Session ID: s1" in text


def test_export_in_gateway_explains_cli_only(env):
    out = env.plugin._cmd_show("export 5 5")
    assert "CLI" in out


def test_storage_lock_uses_msvcrt_without_fcntl(env, monkeypatch):
    import types
    calls = []
    fake = types.SimpleNamespace(LK_NBLCK=1, LK_UNLCK=0,
                                 locking=lambda fd, mode, n: calls.append((mode, n)))
    monkeypatch.setattr(env.plugin, "fcntl", None)
    monkeypatch.setattr(env.plugin, "msvcrt", fake, raising=False)
    with env.plugin._storage_lock():
        pass
    assert calls == [(1, 1), (0, 1)]


def test_deny_texts_point_to_new_session(env):
    assert "export" not in env.plugin._deny_text(session_id="s1")
    env.settings["cli"] = "block"
    env.approval = lambda *a, **k: "deny"
    seed_session(env, model="p/other", ago=5)
    out, called, _ = run_middleware(env, model="m1")
    assert not called and "start /new and paste it. Session ID: s1" in out.choices[0].message.content


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
        assert env.plugin._is_cli_platform({"platform": platform})


def test_invalid_desktop_value_is_rejected(env):
    env.config = {"cli": "block", "desktop": "maybe"}
    cfg = env.plugin._cfg()
    assert cfg["desktop"] == "warn" and cfg["cli"] == "block"
    assert "health: degraded" in env.plugin._health_text()
    env.config = {"desktop": "off"}
    assert env.plugin._cfg()["desktop"] == "off"


def test_debug_timeline_flags_model_change_and_cold(env):
    rows = [
        _journal_row("s1", ts=1000.0, model="a/x", gap_s=None, cause="start", prompt=50000, cache_read_tokens=0),
        _journal_row("s1", ts=1100.0, model="b/y", gap_s=100.0, cause="model_switch", prompt=50000, cache_read_tokens=0),
    ]
    env.plugin._load_observations = lambda: rows
    out = env.plugin._cmd_debug([])
    assert "session s1" in out and "MODEL CHANGE from a/x" in out and "NON-EVALUABLE" in out


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


def _fill_journal(env, n):
    path = env.plugin._obs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"ts": float(i), "n": i}) + "\n" for i in range(n)), encoding="utf-8")
    return path


def _journal_ns(path):
    return [json.loads(ln)["n"] for ln in path.read_text(encoding="utf-8").splitlines()]


def test_trim_keeps_newest_rows(env):
    path = _fill_journal(env, 1200)
    env.plugin._trim_observations(1000)
    assert _journal_ns(path) == list(range(200, 1200))


def test_trim_only_above_ten_percent_margin(env):
    path = _fill_journal(env, 1100)
    env.plugin._trim_observations(1000)
    assert len(_journal_ns(path)) == 1100
    path = _fill_journal(env, 1101)
    env.plugin._trim_observations(1000)
    assert len(_journal_ns(path)) == 1000


def test_trim_survives_write_error(env, monkeypatch):
    path = _fill_journal(env, 1200)
    def fail(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(env.plugin.os, "replace", fail)
    env.plugin._trim_observations(1000)
    assert len(_journal_ns(path)) == 1200
    assert not list(path.parent.glob("*.tmp"))


# ------------------------------------------------- deny text / legacy key ---
def test_deny_text_cancelled_and_timeout_forms(env):
    first = lambda **kw: env.plugin._deny_text("s1", **kw).splitlines()[0]
    assert first(gap_s=1800) == "[CACHEGUARD] BLOCKED · message not sent after a pause of 30 minutes (cancelled)"
    assert first(gap_s=1800, outcome="timeout", timeout=300) == (
        "[CACHEGUARD] BLOCKED · message not sent after a pause of 30 minutes and 300s timeout")
    assert first(cause="model changed") == "[CACHEGUARD] BLOCKED · message not sent (model changed)"

def test_deny_text_when_prompt_could_not_be_delivered(env, monkeypatch):
    first = lambda **kw: env.plugin._deny_text("s1", **kw).splitlines()[0]
    assert first(gap_s=1800, outcome="undelivered", timeout=300) == (
        "[CACHEGUARD] BLOCKED · message not sent after a pause of 30 minutes and the prompt could not be delivered")
    assert first(outcome="undelivered", timeout=300) == (
        "[CACHEGUARD] BLOCKED · message not sent · the prompt could not be delivered")
    wait = types.ModuleType("tools.approval_gateway_wait")
    wait._await_gateway_decision = lambda *a: {"resolved": False, "choice": None, "notify_failed": True}
    monkeypatch.setitem(sys.modules, "tools.approval_gateway_wait", wait)
    monkeypatch.setattr(env.plugin, "_gateway_approval", lambda: ("key", lambda *a: None))
    deny = lambda outcome, timeout: env.plugin._deny_text("s1", gap_s=1800, outcome=outcome, timeout=timeout)
    out = env.plugin._gateway_confirmation({"session_id": "s1"}, "cmd", deny, "m")
    text = out.choices[0].message.content
    assert "could not be delivered" in text and "timeout" not in text


@pytest.mark.parametrize("answer,tail", [("deny", "(cancelled)"), ("timeout", "and 300s timeout")])
def test_block_reports_cancel_or_timeout(env, answer, tail):
    env.config["cli"] = "block"
    seed_session(env, ago=3600)
    env.approval = lambda *a, **k: answer
    out, called, _ = run_middleware(env)
    first = out.choices[0].message.content.splitlines()[0]
    assert not called and first.startswith("[CACHEGUARD] BLOCKED · message not sent after a pause of 60 minutes") and first.endswith(tail)


def test_mode_is_not_a_setting(env):
    env.config.update(mode="block")
    cfg = env.plugin._cfg()
    assert (cfg["cli"], cfg["gateway"]) == (env.plugin.DEFAULTS["cli"], env.plugin.DEFAULTS["gateway"])
    assert env.plugin._parse_setting("mode", "warn")[0] is None


def test_mode_values_are_not_reshaped(env):
    env.config.update(cli=" Block ", gateway="WARN")
    cfg = env.plugin._cfg()
    assert (cfg["cli"], cfg["gateway"]) == ("warn", "off")  # defaults: neither value is a mode
    assert "health: degraded" in env.plugin._health_text()


def test_parse_setting_group_modes(env):
    assert env.plugin._parse_setting("cli", "warn")[0] == ("cli", "warn", "warn")
    assert env.plugin._parse_setting("desktop", "block")[0] == ("desktop", "block", "block")
    assert env.plugin._parse_setting("desktop", "maybe")[0] is None
    for gone in ("show_model", "model_change", "max_observations"):
        assert env.plugin._parse_setting(gone, "on")[0] is None


def test_export_skips_new_style_denials(env):
    _seed_chat(env, 4)
    con = env.db()
    con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                ("s1", "assistant", "[CACHEGUARD] BLOCKED · message not sent after a pause of 5 minutes (cancelled)", 50.0))
    con.commit()
    con.close()
    _set_export_cli(env, "s1")
    out = env.plugin._cmd_show("export all")
    assert "4 of 4 messages" in out


# ------------------------------------------------- final message format
def test_kept_result_line_leaves_out_the_estimate_shown_before_sending(env, monkeypatch):
    env.config.update(cli="warn")
    _cold_history(env.plugin, cold=6, warm=4, gap=3600)
    _guard_acts(env, monkeypatch)
    _reply(env)
    assert env.posted == [RESULT_LINE]


def test_block_result_line_has_no_warn_marker_and_keeps_estimate(env):
    env.config["cli"] = "block"
    seed_session(env, ago=3600)
    _cold_history(env.plugin, cold=6, warm=4)
    env.approval = lambda *a, **k: "once"
    run_middleware(env)
    note = env.plugin._PENDING.get("s1", "")
    assert note.startswith("CGUARD ") and "WARN" not in note and "similar" not in note


def test_denial_has_no_estimate(env):
    text = env.plugin._deny_text("s1", gap_s=60)
    assert text == ("[CACHEGUARD] BLOCKED · message not sent after a pause of 1 minute (cancelled)\n"
                    "Send it again, or start /new and paste it. Session ID: s1")
    assert "lost" not in text and "%" not in text
    assert "session_search" not in text


def test_model_change_box_is_affirmative_and_names_the_new_model(env):
    env.config.update(cli="block")
    seed_session(env, ago=10, model="p/old")
    prompts = []
    env.approval = lambda command, desc, **kw: (prompts.append(command), "deny")[1]
    out, called, _ = run_middleware(env, model="new")
    assert not called
    assert prompts[0].startswith("[CACHEGUARD] Model changed\n\np/old → p/new · ~")
    assert prompts[0].endswith("tokens may be rebilled") and "full price" not in prompts[0]
    assert out.choices[0].message.content.splitlines()[0] == "[CACHEGUARD] BLOCKED · message not sent (model changed)"


def test_model_change_result_has_no_pause_or_estimate(env):
    row = {"evaluable": False, "cause": "model_switch", "gap_s": 3600.0, "prompt_tokens": 30000,
           "cache_read_tokens": 0}
    assert env.plugin._outcome_text(row, ("p/old", "p/new")) == "cache lost, 30k rebilled, model changed"


def test_saved_history_filter_skips_blocked_texts(env):
    assert "BLOCKED · message not sent" in env.plugin._NOT_DENY_SQL


@pytest.mark.parametrize("seconds,text", [
    (30, "30 seconds"), (60, "1 minute"), (3600, "60 minutes"), (179 * 60, "179 minutes"),
    (5 * 3600, "5 hours"), (47 * 3600, "47 hours"), (5 * 86400, "5 days"),
])
def test_pause_text_uses_hours_and_days_for_long_pauses(env, seconds, text):
    assert env.plugin._pause_text(seconds) == text


def test_superscript_digits_in_command_args_do_not_crash(env):
    assert "usage" in env.plugin._cmd_export(["²", "1"]).lower()
    assert isinstance(env.plugin._cmd_debug(["²"]), str)
    assert "number" in env.plugin._cmd_show("set tokens ²")


# ------------------------------------------------------- surface groups ---
@pytest.mark.parametrize("platform", ["cli", "tui", "local", "desktop", "webui", "acp"])
def test_cli_group_platforms_use_cli_setting(env, platform):
    env.config.update(cli="warn", gateway="off", size_k=1)
    seed_session(env, ago=3600)
    out, called, sentinel = run_middleware(env, platform=platform)
    assert called and out is sentinel and env.warned() is not None


def test_desktop_block_prompts_through_gateway_queue(env, monkeypatch):
    env.config.update(desktop="block", gateway="off", size_k=1)
    seed_session(env, ago=3600)
    blocked = object()
    monkeypatch.setattr(env.plugin, "_healthy", lambda _name: True)
    monkeypatch.setattr(env.plugin, "_gateway_approval", lambda: ("key", lambda *a: None))
    monkeypatch.setattr(env.plugin, "_gateway_confirmation", lambda *a: blocked)
    out, called, _ = run_middleware(env, platform="desktop")
    assert out is blocked and not called


@pytest.mark.parametrize("platform", ["telegram", "discord", "some-new-chat-app"])
def test_messaging_and_unknown_platforms_use_gateway_setting(env, platform):
    env.config.update(cli="block", gateway="warn", size_k=1)
    seed_session(env, ago=3600)
    env.approval = lambda *a, **k: pytest.fail("gateway group must not use the CLI box")
    out, called, sentinel = run_middleware(env, platform=platform)
    assert called and out is sentinel and env.warned() is not None


@pytest.mark.parametrize("platform", ["cron", "kanban", "subagent", "api_server", "webhook", "msgraph_webhook"])
@pytest.mark.parametrize("mode", ["warn", "block"])
def test_unattended_platforms_are_pure_pass_through(env, platform, mode):
    env.config.update(cli=mode, gateway=mode, size_k=1)
    env.approval = lambda *a, **k: pytest.fail("unattended must never prompt")
    seed_session(env, ago=3600)
    before = env.plugin._read_session("s1")
    out, called, sentinel = run_middleware(env, platform=platform, model="new")
    assert called and out is sentinel
    assert env.warned() is None and "s1" not in env.plugin._PENDING
    assert env.plugin._read_observations() == [] and env.plugin._read_session("s1") == before
    env.plugin._PENDING["s1"] = "[CACHEGUARD] stale"
    assert env.plugin._transform_llm_output(
        response_text="hi", session_id="s1", platform=platform) is None


@pytest.mark.parametrize("var, value", [("HERMES_KANBAN_TASK", "t_42"), ("HERMES_CRON_SESSION", "1")])
def test_kanban_and_cron_workers_on_cli_are_left_alone(env, monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    env.config.update(cli="block", size_k=1)
    env.approval = lambda *a, **k: pytest.fail("unattended must never prompt")
    seed_session(env, ago=3600)
    out, called, sentinel = run_middleware(env, platform="cli", model="new")
    assert called and out is sentinel
    assert env.warned() is None and env.plugin._read_observations() == []


# ------------------------------------------------- one line per reply ---



# ----------------------------------------------- CLI box and gateway decisions ---
def _cli_owner(env, answer):
    import threading

    class Owner:
        _app = object()
        _approval_lock = threading.Lock()

        def _prompt_text_input_modal(self, *, title, detail, choices, timeout):
            return answer

        def cb(self, *a, **k):
            raise AssertionError("the modal must be used, not the plain callback")

    env.approval = Owner().cb


@pytest.mark.parametrize("answer,sent,word", [
    ("once", True, None), ("deny", False, "(cancelled)"), (None, False, "timeout"),
    ("session", False, "(cancelled)"), ("always", False, "(cancelled)"), ("", False, "(cancelled)"),
])
def test_cli_box_only_send_message_sends(env, answer, sent, word):
    _cli_owner(env, answer)
    deny = lambda outcome, timeout: env.plugin._deny_text("s1", gap_s=1800, outcome=outcome, timeout=timeout)
    handled, response = env.plugin._confirmation_screen({}, "title\n\ndetail", deny, "m")
    assert handled
    if sent:
        assert response is None
    else:
        text = response.choices[0].message.content
        assert "message not sent" in text and word in text
        assert ("timeout" in text) == (answer is None)


def _hermes_like_owner(env, answer, helper=True):
    """Owner with Hermes' real answer reader (same aliases) so typed answers can be tested."""
    import threading

    aliases = {"1": "once", "once": "once", "approve": "once", "yes": "once", "y": "once", "ok": "once",
               "3": "cancel", "cancel": "cancel", "no": "cancel", "n": "cancel"}

    class Owner:
        _app = object()
        _approval_lock = threading.Lock()

        def _prompt_text_input_modal(self, *, title, detail, choices, timeout):
            return answer

        def cb(self, *a, **k):
            raise AssertionError("the modal must be used, not the plain callback")

    if helper:
        def normalize(self, raw, choices):
            raw = raw.strip().lower()
            allowed = {c[0] for c in choices}
            if aliases.get(raw) in allowed:
                return aliases[raw]
            return raw if raw in allowed else None
        Owner._normalize_slash_confirm_choice = normalize
    env.approval = Owner().cb


def _box(env):
    deny = lambda outcome, timeout: env.plugin._deny_text("s1", gap_s=1800, outcome=outcome, timeout=timeout)
    return env.plugin._confirmation_screen({}, "title\n\ndetail", deny, "m")


@pytest.mark.parametrize("answer", ["once", "y", " Yes ", "1", "OK", "approve"])
def test_cli_box_typed_send_answers_send(env, answer):
    _hermes_like_owner(env, answer)
    handled, response = _box(env)
    assert handled and response is None


@pytest.mark.parametrize("answer", ["deny", "cancel", "no", "n", "3"])
def test_cli_box_typed_cancel_answers_cancel_without_degraded_line(env, answer):
    _hermes_like_owner(env, answer)
    handled, response = _box(env)
    assert handled and "(cancelled)" in response.choices[0].message.content
    assert not env.plugin._H["noticed"]


@pytest.mark.parametrize("answer", ["maybe", "", "  ", "always", "sendit", "send", "2"])
def test_cli_box_unreadable_answer_sends_nothing_and_shows_degraded_line(env, answer):
    _hermes_like_owner(env, answer)
    handled, response = _box(env)
    assert handled and "message not sent" in response.choices[0].message.content
    assert [n for n in env.plugin._H["noticed"] if "cacheguard box" in n]


def _cb_box(env, answer, helper_owner=False):
    env.approval = lambda *a, **k: answer
    env.config["cli"] = "block"
    seed_session(env, ago=3600)
    return _run_counting(env)


@pytest.mark.parametrize("answer", ["weird", "", 7, {"x": 1}, ["once"], True])
def test_cli_unknown_answer_value_sends_once_and_switches_the_box_off(env, answer):
    out, calls, sentinel = _cb_box(env, answer)
    assert calls == 1 and out is sentinel
    assert len(_box_broke_notices(env)) == 1 and env.plugin._H["box_failed"] == "ValueError"


@pytest.mark.parametrize("answer,word", [("deny", "(cancelled)"), ("timeout", "timeout")])
def test_cli_cancel_and_timeout_stay_not_sent(env, answer, word):
    out, calls, _ = _cb_box(env, answer)
    assert calls == 0 and word in out.choices[0].message.content
    assert not _box_broke_notices(env)


@pytest.mark.parametrize("answer", ["once", "session", "always"])
def test_cli_known_send_values_send_once_without_box_failure(env, answer):
    out, calls, sentinel = _cb_box(env, answer)
    assert calls == 1 and out is sentinel and not _box_broke_notices(env)


@pytest.mark.parametrize("answer", [7, {"x": 1}, ["once"]])
def test_cli_modal_non_text_answer_sends_once_and_switches_the_box_off(env, answer):
    _hermes_like_owner(env, answer)
    env.config["cli"] = "block"
    seed_session(env, ago=3600)
    out, calls, sentinel = _run_counting(env)
    assert calls == 1 and out is sentinel and len(_box_broke_notices(env)) == 1


def test_cli_modal_timeout_is_not_sent(env):
    _hermes_like_owner(env, None)
    env.config["cli"] = "block"
    seed_session(env, ago=3600)
    out, calls, _ = _run_counting(env)
    assert calls == 0 and not _box_broke_notices(env)


def test_cli_box_unreadable_answer_degraded_line_shows_once(env):
    _hermes_like_owner(env, "maybe")
    _box(env)
    _box(env)
    assert len([n for n in env.plugin._H["noticed"] if "cacheguard box" in n]) == 1


def test_cli_box_without_hermes_helper_keeps_exact_match(env):
    _hermes_like_owner(env, "y", helper=False)
    handled, response = _box(env)
    assert handled and "message not sent" in response.choices[0].message.content
    _hermes_like_owner(env, "send", helper=False)
    handled, response = _box(env)
    assert handled and "message not sent" in response.choices[0].message.content
    _hermes_like_owner(env, "once", helper=False)
    assert _box(env) == (True, None)


def test_cli_box_helper_that_raises_keeps_exact_match(env):
    _hermes_like_owner(env, "once")
    owner = env.approval.__self__
    owner._normalize_slash_confirm_choice = lambda raw, choices: 1 / 0
    assert _box(env) == (True, None)


def test_health_checks_the_answer_helper(env):
    assert "confirm_choice" in env.plugin._health()


@pytest.mark.parametrize("decision,sent,word", [
    ({"resolved": True, "choice": "once"}, True, None),
    ({"resolved": True, "choice": "session"}, True, None),
    ({"resolved": True, "choice": "always"}, True, None),
    ({"resolved": True, "choice": "deny"}, False, "(cancelled)"),
    ({"resolved": True, "choice": None, "cancelled": "turn interrupted"}, False, "(cancelled)"),
    ({"resolved": True, "choice": None}, False, "(cancelled)"),
    ({"resolved": False, "choice": None}, False, "timeout"),
    ({"resolved": False, "choice": None, "notify_failed": True}, False, "could not be delivered"),
])
def test_gateway_decision_only_explicit_allow_sends(env, monkeypatch, decision, sent, word):
    wait = types.ModuleType("tools.approval_gateway_wait")
    wait._await_gateway_decision = lambda *a: decision
    monkeypatch.setitem(sys.modules, "tools.approval_gateway_wait", wait)
    monkeypatch.setattr(env.plugin, "_gateway_approval", lambda: ("key", lambda *a: None))
    deny = lambda outcome, timeout: env.plugin._deny_text("s1", gap_s=1800, outcome=outcome, timeout=timeout)
    out = env.plugin._gateway_confirmation({"session_id": "s1"}, "cmd", deny, "m")
    if sent:
        assert out is None
    else:
        assert word in out.choices[0].message.content


def test_storage_lock_gives_up_instead_of_waiting(env, monkeypatch):
    import fcntl
    import time

    monkeypatch.setattr(env.plugin, "_LOCK_WAIT_S", 0.1)
    with open(env.plugin._state_dir() / ".lock", "a+") as other:
        fcntl.flock(other.fileno(), fcntl.LOCK_EX)
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            with env.plugin._storage_lock():
                pass
        assert time.monotonic() - start < 2
    with env.plugin._storage_lock():  # free again
        pass


@pytest.mark.parametrize("name, symbol", [
    ("usage_api", "normalize_usage"), ("session_key", "get_current_session_key"),
    ("write_origin", "get_current_write_origin")])
def test_health_checks_the_other_hermes_symbols(env, monkeypatch, name, symbol):
    monkeypatch.setattr(env.plugin, "_has_symbol", lambda mod, sym: True)
    assert env.plugin._health()[name][0] is True
    monkeypatch.setattr(env.plugin, "_has_symbol", lambda mod, sym: sym != symbol)
    assert env.plugin._health()[name][0] is False
    env.plugin._run_health()
    assert name in env.plugin._health_text()


def test_setup_refuses_pause_under_one_minute(env):
    ctx = _with_ctx(env)
    out = env.plugin._cmd_show("setup all warn 0.5 30")
    assert out.startswith("cacheguard: pause_m must be a number >= 1.") and ctx.calls == []


def test_help_example_sets_one_group(env):
    assert "/cacheguard setup cli block 10 20" in env.plugin._HELP_TEXT
    assert "setup all block 10 30" not in env.plugin._HELP_TEXT


# ------------------------------------------- llm_request: lines kept out of the model's view --
def _note(p):
    return "\n\n" + p._PREFIX + " result: 5k reused"


def _blocked(p):
    return p._deny_text("s1", gap_s=700.0)


def _req(p, msgs, key="messages"):
    return p._on_llm_request(request={"model": "m", key: msgs}, session_id="s1", platform="cli")


def test_request_note_removed_and_model_text_kept(env):
    p = env.plugin
    own = "Hello  \n\n  world\u00e9 \n- x"
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": own + _note(p)}]
    out = _req(p, msgs)["request"]["messages"]
    assert out[1]["content"] == own and out[0] is msgs[0]
    assert msgs[1]["content"].endswith("result: 5k reused")  # input untouched


def test_prefix_is_cacheguard_and_not_denied_sql_matches_it(env):
    p = env.plugin
    assert p._PREFIX == "[CACHEGUARD]"
    assert "BLOCKED · message not sent" in p._deny_text("s1", gap_s=60)
    assert "%BLOCKED · message not sent%" in p._NOT_DENY_SQL


def test_request_blocked_pair_removed_and_normal_pairs_kept(env):
    p = env.plugin
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "A"},
            {"role": "user", "content": "big"}, {"role": "assistant", "content": _blocked(p)},
            {"role": "user", "content": "b"}, {"role": "assistant", "content": "B"}]
    out = _req(p, msgs)["request"]["messages"]
    assert [m["content"] for m in out] == ["a", "A", "b", "B"]


def test_request_blocked_without_user_before_removes_only_assistant(env):
    p = env.plugin
    msgs = [{"role": "assistant", "content": "A"}, {"role": "assistant", "content": _blocked(p)},
            {"role": "user", "content": "b"}]
    out = _req(p, msgs)["request"]["messages"]
    assert [m["content"] for m in out] == ["A", "b"]


def test_request_is_idempotent_and_prefix_stable(env):
    p = env.plugin
    h1 = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "A" + _note(p)},
          {"role": "user", "content": "big"}, {"role": "assistant", "content": _blocked(p)}]
    h2 = h1 + [{"role": "user", "content": "c"}, {"role": "assistant", "content": "C" + _note(p)}]
    c1 = _req(p, h1)["request"]["messages"]
    assert _req(p, c1) is None  # already clean
    c2 = _req(p, h2)["request"]["messages"]
    assert c2[:len(c1)] == c1 and c1 == _req(p, h1)["request"]["messages"]


def test_request_user_message_with_label_kept(env):
    p = env.plugin
    msgs = [{"role": "user", "content": p._PREFIX + " BLOCKED · message not sent"},
            {"role": "assistant", "content": "ok"}]
    assert _req(p, msgs) is None


def test_request_error_leaves_request_unchanged(env, monkeypatch):
    p = env.plugin
    monkeypatch.setattr(p, "_clean_history", lambda items: 1 / 0)
    assert _req(p, [{"role": "user", "content": "a"}]) is None


def test_request_unattended_untouched(env):
    p = env.plugin
    msgs = [{"role": "assistant", "content": "A" + _note(p)}]
    assert p._on_llm_request(request={"messages": msgs}, session_id="s1", platform="cron") is None


def test_request_part_lists_chat_and_anthropic(env):
    p = env.plugin
    parts = lambda t: [{"type": "text", "text": t}]
    msgs = [{"role": "user", "content": parts("a")}, {"role": "assistant", "content": parts("A" + _note(p))},
            {"role": "user", "content": parts("big")}, {"role": "assistant", "content": parts(_blocked(p))}]
    out = _req(p, msgs)["request"]["messages"]
    assert out == [msgs[0], {"role": "assistant", "content": parts("A")}]


def test_request_codex_input_shapes(env):
    p = env.plugin
    ot = lambda t: [{"type": "output_text", "text": t}]
    items = [{"role": "user", "content": "a"},
             {"type": "message", "role": "assistant", "status": "completed", "content": ot("A" + _note(p))},
             {"role": "user", "content": [{"type": "input_text", "text": "big"}]},
             {"type": "message", "role": "assistant", "content": ot(_blocked(p))},
             {"type": "function_call_output", "call_id": "c", "output": "x"}]
    out = _req(p, items, key="input")["request"]["input"]
    assert out == [items[0], {**items[1], "content": ot("A")}, items[4]]


def test_request_tool_result_before_blocked_is_kept(env):
    p = env.plugin
    msgs = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "r"}]},
            {"role": "assistant", "content": _blocked(p)}]
    out = _req(p, msgs)["request"]["messages"]
    assert len(out) == 1 and out[0]["role"] == "user"


def test_request_health_check_listed(env):
    assert "request_middleware" in env.plugin._health()


# ------------------------------------------------- fail-open paths ----------


def test_error_before_the_box_calls_provider_exactly_once(env, monkeypatch):
    _block_setup(env, "deny")
    monkeypatch.setattr(env.plugin, "_evaluate", lambda *a, **k: 1 / 0)
    calls, sentinel = [], object()

    def next_call():
        calls.append(1)
        return sentinel

    out = env.plugin._on_llm_execution(
        next_call=next_call, request=big_request(), session_id="s1", model="m1", provider="p",
        api_call_count=1, platform="cli", api_mode="chat_completions")
    assert out is sentinel and len(calls) == 1


def test_block_with_unhealthy_approval_cb_warns_without_prompting(env, monkeypatch):
    calls = _block_setup(env, "deny")  # a callback exists, but the register-time check failed
    monkeypatch.setitem(env.plugin._H["checks"], "approval_cb", (False, "no callback"))
    out, called, sentinel = run_middleware(env)
    assert called and out is sentinel and not calls
    assert env.warned() is not None


@pytest.mark.parametrize("value, expected", [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("0", False), ("", False), ("no", False),
])
def test_single_query_env_fallback(env, monkeypatch, value, expected):
    # No tools.approval_context in the fakes: the import fails and the env marker decides.
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", value)
    assert env.plugin._single_query() is expected


def _fake_gateway_session_context(monkeypatch, get_session_env):
    import types as _types

    monkeypatch.setitem(sys.modules, "gateway", _types.ModuleType("gateway"))
    ctx = _types.ModuleType("gateway.session_context")
    ctx.get_session_env = get_session_env
    monkeypatch.setitem(sys.modules, "gateway.session_context", ctx)


def test_session_env_uses_gateway_session_value_over_os_environ(env, monkeypatch):
    _fake_gateway_session_context(monkeypatch, lambda name, default="": {"HERMES_SESSION_KEY": "k1"}.get(name, default))
    monkeypatch.setenv("HERMES_SESSION_KEY", "from-environ")
    assert env.plugin._session_env("HERMES_SESSION_KEY") == "k1"
    assert env.plugin._session_env("UNSET_NAME_FOR_TEST") == ""


def test_session_env_falls_back_to_os_environ_when_gateway_read_raises(env, monkeypatch):
    def broken(name, default=""):
        raise RuntimeError("no session context")

    _fake_gateway_session_context(monkeypatch, broken)
    monkeypatch.setenv("HERMES_SESSION_KEY", "from-environ")
    assert env.plugin._session_env("HERMES_SESSION_KEY") == "from-environ"


def test_housekeeping_keeps_newest_session_files(env, monkeypatch):
    import os

    monkeypatch.setattr(env.plugin, "_MAX_SESSION_FILES", 3)
    d = env.plugin._sessions_dir()
    now = time.time()
    other = d / "other.txt"
    other.write_text("{}")
    files = [d / f"s{i}.json" for i in range(5)]
    for i, f in enumerate(files):  # s0 is the oldest, s4 the newest
        f.write_text("{}")
        os.utime(f, (now - 1000 + i * 10,) * 2)
    env.plugin._sessions_housekeeping()
    assert [f.exists() for f in files] == [False, False, True, True, True]
    assert other.exists()


def test_housekeeping_runs_once_per_process(env, monkeypatch):
    import os

    monkeypatch.setattr(env.plugin, "_MAX_SESSION_FILES", 1)
    d = env.plugin._sessions_dir()
    env.plugin._sessions_housekeeping()
    for i, name in enumerate(("a.json", "b.json")):
        f = d / name
        f.write_text("{}")
        os.utime(f, (time.time() - 100 + i,) * 2)
    env.plugin._sessions_housekeeping()
    assert (d / "a.json").exists() and (d / "b.json").exists()


# --- health: real symbol check ---------------------------------------------------------------

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
    assert env.plugin._has_symbol("tools.bot_mode_probe", "is_bot_mode_managed") is True
    assert env.plugin._has_symbol("tools.bot_mode_probe", "gone") is False


_NEW_CHECKS = [
    ("session_env", "gateway.session_context", "get_session_env"),
    ("bot_chat", "tools.bot_mode_probe", "BOT_CHAT_TITLE"),
    ("bot_chat", "tools.bot_mode_probe", "is_bot_mode_managed"),
    ("single_query", "tools.approval_context", "_is_single_query_approval_context"),
    ("approval_timeout", "tools.approval_context", "_get_approval_timeout"),
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


def test_state_db_sessions_table_is_checked(env):
    import sqlite3

    env.db().close()
    assert env.plugin._health()["sessions_db"] == (True, "")
    con = sqlite3.connect(env.home / "state.db")
    con.execute("DROP TABLE sessions")
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)")
    con.commit()
    con.close()
    checks = env.plugin._health()
    ok, detail = checks["sessions_db"]
    assert not ok and "sessions" in detail and "session_key" in detail
    assert checks["state_db"] == (True, "")  # the history fallback and model lookup stay on


def test_renamed_symbol_prints_a_degraded_line_and_the_message_is_still_sent(env, monkeypatch):
    monkeypatch.setattr(env.plugin, "_has_symbol", lambda mod, sym: sym != "get_session_env")
    env.plugin._run_health()
    assert any(line.endswith("cacheguard may treat cron and kanban runs as chats.") for line in env.notices)
    assert not any("degraded" in line or "session_env" in line for line in env.notices)
    _out, called, _sentinel = run_middleware(env, request={"messages": [{"role": "user", "content": "hi"}], "tools": []})
    assert called is True


# --- box timeout follows Hermes ----------------------------------------------------------------

def _fake_approval_context(monkeypatch, fn):
    monkeypatch.setitem(sys.modules, "tools.approval_context",
                        types.SimpleNamespace(_get_approval_timeout=fn))


def test_approval_timeout_uses_the_hermes_helper(env, monkeypatch):
    monkeypatch.setattr(env.plugin, "_raw_config", lambda: {"approvals": {"timeout": 99999999999}})
    _fake_approval_context(monkeypatch, lambda: 31536000)
    assert env.plugin._approval_timeout() == 31536000
    text = env.plugin._denied(lambda outcome, timeout: env.plugin._deny_text(outcome="timeout", timeout=timeout), "timeout")
    assert "31536000s timeout" in text


def test_approval_timeout_falls_back_when_the_helper_is_missing_or_fails(env, monkeypatch):
    monkeypatch.setattr(env.plugin, "_raw_config", lambda: {"approvals": {"timeout": 42}})
    assert env.plugin._approval_timeout() == 42  # no helper in the fake Hermes
    _fake_approval_context(monkeypatch, lambda: 1 / 0)
    assert env.plugin._approval_timeout() == 42
    monkeypatch.setattr(env.plugin, "_raw_config", lambda: 1 / 0)
    assert env.plugin._approval_timeout() == 300


# --- big journal: read only its end, trim rarely -------------------------------------------------

def _journal_row(sid="other", ts=1.0, prompt=1000, **extra):
    row = {"ts": ts, "session_id": sid, "model": "p/m1", "api_mode": "chat_completions",
           "api_request_id": f"r{ts}", "gap_s": 5.0, "prompt_tokens": prompt,
           "cache_read_tokens": prompt, "cache_write_tokens": 0,
           "prev_prompt_tokens": prompt, "cause": None}
    row.update(extra)
    return row


def _write_journal(env, rows, tail_text=""):
    path = env.plugin._obs_path()
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + tail_text, encoding="utf-8")
    return path


def _filler(n, start=1000.0):
    return [_journal_row(ts=start + i) for i in range(n)]


def _past_the_tail(env):
    """Enough filler rows to push everything before them out of the tail read."""
    return _filler(env.plugin._TAIL_BYTES // 250 + 200)


def _payload(**extra):
    return dict({"session_id": "s", "provider": "p", "model": "m1", "api_mode": "chat_completions",
                 "api_request_id": "req"}, **extra)


def _respond(env, ts, prompt=1000, cached=1000, **payload):
    return env.plugin._record_response(
        _payload(**payload), {"usage": {"prompt_tokens": prompt, "cache_read_tokens": cached}}, ts - 1, ts)


def test_tail_read_survives_a_damaged_last_line_and_a_cut_first_line(env):
    put_session(env.plugin, "s", {"last_api_ts": 10.0, "model": "p/m1", "api_mode": "chat_completions"})
    _write_journal(env, _past_the_tail(env) + [_journal_row("s", ts=5000.0, prompt=7000)],
                   tail_text='{"ts": 1, "session_id": "s", "mod')
    rows, truncated = env.plugin._read_recent_observations()
    assert truncated and rows[-1]["ts"] == 5000.0 and all(isinstance(r, dict) for r in rows)


def test_session_read_does_not_read_the_whole_journal(env, monkeypatch):
    _write_journal(env, _past_the_tail(env))
    monkeypatch.setattr(env.plugin, "_read_observations", lambda: pytest.fail("whole journal read"))
    env.plugin._get_session("s")
    env.plugin._remember_send(_payload(), 1.0)


def test_record_finds_the_previous_row_even_when_it_is_older_than_the_tail(env):
    put_session(env.plugin, "s", {"last_api_ts": 100.0, "model": "p/m1", "api_mode": "chat_completions",
                                  "prompt_tokens": 1000})
    _write_journal(env, [_journal_row("s", ts=100.0, prompt=1000)] + _past_the_tail(env))
    verdict = _respond(env, 200.0, prompt=1100, cached=1000)
    assert verdict["cause"] is None and verdict["prev_prompt_tokens"] == 1000 and verdict["gap_s"] == 99.0
    assert env.plugin._load_observations()[-1]["cause"] is None


def test_record_counts_early_positive_reads_for_a_zero_read(env):
    # A read of 0 is judged only after 3 earlier positive reads of the model: here they are older than the tail.
    put_session(env.plugin, "s", {"last_api_ts": 100.0, "model": "p/m1", "api_mode": "chat_completions",
                                  "prompt_tokens": 1000})
    early = [_journal_row("x", ts=float(i), prompt=1000) for i in range(1, 4)]
    _write_journal(env, early + [_journal_row("s", ts=100.0)] + _past_the_tail(env))
    assert env.plugin._read_recent_observations()[1]
    verdict = _respond(env, 200.0, prompt=1000, cached=0)
    assert verdict["evaluable"] is True and verdict["cold"] is True


def test_trim_keeps_the_journal_near_its_limit_across_many_records(env, monkeypatch):
    monkeypatch.setattr(env.plugin, "_MAX_OBSERVATIONS", 1000)
    path = _write_journal(env, _filler(1400))  # over the limit when the process starts
    for i in range(400):
        _respond(env, 10_000.0 + i, session_id=f"s{i % 3}", api_request_id=f"q{i}")
        assert len(path.read_text(encoding="utf-8").splitlines()) <= 1000 * 1.1 + env.plugin._TRIM_EVERY
    lines = path.read_text(encoding="utf-8").splitlines()
    assert 1000 <= len(lines) <= 1000 * 1.1 + env.plugin._TRIM_EVERY
    assert json.loads(lines[-1])["api_request_id"] == "q399"  # the newest row is kept


def test_trim_runs_on_the_first_record_and_then_only_every_few(env, monkeypatch):
    monkeypatch.setattr(env.plugin, "_MAX_OBSERVATIONS", 1000)
    _write_journal(env, _filler(1000))
    calls = []
    original = env.plugin._trim_observations
    monkeypatch.setattr(env.plugin, "_trim_observations", lambda limit: calls.append(1) or original(limit))
    for i in range(env.plugin._TRIM_EVERY):
        _respond(env, 10_000.0 + i, api_request_id=f"q{i}")
    assert len(calls) == 1
    _respond(env, 20_000.0, api_request_id="next")
    assert len(calls) == 2


# --- notices: posted outside the model context -------------------------------- #
RESULT_LINE = "CGUARD OK 59k reused, 60 min"


def test_result_line_is_posted_and_the_reply_is_left_alone(env, monkeypatch):
    _guard_acts(env, monkeypatch)
    assert _reply(env) is None
    assert env.posted == [RESULT_LINE]
    assert "s1" not in env.plugin._PENDING


def test_result_line_is_posted_once(env, monkeypatch):
    _guard_acts(env, monkeypatch)
    _reply(env)
    _reply(env)
    assert env.posted == [RESULT_LINE]


def test_note_waits_for_its_own_turn(env, monkeypatch):
    real = env.plugin._queue_note
    monkeypatch.setattr(env.plugin, "_queue_note", lambda sid, text, turn_id=None: real(sid, text, "t1"))
    _guard_acts(env, monkeypatch)
    other = env.plugin._transform_llm_output(
        response_text="side answer", session_id="s1", model="p/m1", platform="cli", turn_id="t2")
    assert other is None and env.posted == [] and "s1" in env.plugin._PENDING
    assert _reply(env) is None and env.posted == [RESULT_LINE]


@pytest.mark.parametrize("failure", [False, RuntimeError("boom")])
def test_post_message_failing_never_raises_and_shows_nothing(env, monkeypatch, failure):
    env.post_ok = failure
    _guard_acts(env, monkeypatch)
    env.plugin._notice("something broke")
    assert _reply(env) is None
    assert "s1" not in env.plugin._PENDING  # no fallback: the line is dropped
    assert env.posted == []


def test_without_post_message_nothing_is_shown_and_health_says_so(env, monkeypatch):
    del env.plugin._CTX.post_message
    _guard_acts(env, monkeypatch)
    env.plugin._run_health()
    assert env.plugin._HEALTH_EFFECTS["post_message"] in env.notices
    assert "post_message: missing or changed" in env.plugin._health_text()
    assert _reply(env) is None and "s1" not in env.plugin._PENDING


def test_notice_is_posted_once_per_session(env):
    env.plugin._notice("something broke")
    assert env.posted == []  # waits for a chat turn
    assert _reply(env, session_id="s1") is None
    assert env.posted == ["CGUARD something broke"]
    _reply(env, session_id="s1")
    assert env.posted == ["CGUARD something broke"]
    _reply(env, session_id="s2")
    assert env.posted == ["CGUARD something broke"] * 2


def test_same_notice_is_queued_once_per_process(env):
    env.plugin._notice("something broke")
    env.plugin._notice("something broke")
    assert env.notices == ["something broke"]


def test_notice_comes_before_the_result_line(env, monkeypatch):
    _guard_acts(env, monkeypatch)
    env.plugin._notice("something broke")
    _reply(env)
    assert env.posted == ["CGUARD something broke", RESULT_LINE]


def test_notice_retries_after_a_failed_post(env):
    env.plugin._notice("something broke")
    env.post_ok = False
    _reply(env)
    env.post_ok = True
    _reply(env)
    assert env.posted == ["CGUARD something broke"]


def test_notice_sessions_are_bounded(env):
    env.plugin._notice("something broke")
    for i in range(env.plugin._PENDING_MAX + 10):
        _reply(env, session_id=f"x{i}")
    assert len(env.plugin._NOTICES_SEEN) == env.plugin._PENDING_MAX


def test_unattended_posts_nothing(env, monkeypatch):
    _guard_acts(env, monkeypatch)
    env.plugin._notice("something broke")
    out = env.plugin._transform_llm_output(
        response_text="hi", session_id="s1", platform="cron", turn_id="t1")
    assert out is None and env.posted == [] and "s1" in env.plugin._PENDING


def test_notices_is_not_a_setting(env):
    assert "notices" not in env.plugin.DEFAULTS and "notices" not in env.plugin._HELP_TEXT
    assert "notices" not in env.plugin._cmd_show("")


# --- short posted lines ------------------------------------------------------- #
@pytest.mark.parametrize("count,text", [
    (0, "0"), (999, "999"), (1000, "1k"), (1499, "1k"), (1500, "2k"), (23_400, "23k"),
    (999_499, "999k"), (999_500, "1.0M"), (1_234_567, "1.2M")])
def test_short_tokens(env, count, text):
    assert env.plugin._short_tokens(count) == text


@pytest.mark.parametrize("seconds,text", [
    (0, "0 s"), (45, "45 s"), (60, "1 min"), (600, "10 min"), (179 * 60, "179 min"),
    (180 * 60, "3 h"), (47 * 3600, "47 h"), (48 * 3600, "2 d")])
def test_compact_pause(env, seconds, text):
    assert env.plugin._compact_pause(seconds) == text


def _short_row(**over):
    row = {"evaluable": True, "cold": False, "prompt_tokens": 60000, "cache_read_tokens": 23000,
           "miss_delta": 0, "gap_s": 60.0, "model": "p/m1", "api_mode": "chat_completions"}
    row.update(over)
    return row


@pytest.mark.parametrize("row,change,text", [
    (_short_row(), None, "23k reused, 1 min"),
    (_short_row(gap_s=None), None, "23k reused"),
    (_short_row(cold=True, miss_delta=23000, gap_s=600.0), None, "cache lost, 23k rebilled, 10 min"),
    (_short_row(cache_read_tokens=58000), ("a/x", "b/y"), "58k reused, model changed"),
    (_short_row(prompt_tokens=None), ("a/x", "b/y"), "cache unknown, model changed"),
    (_short_row(prompt_tokens=60000, cache_read_tokens=0), ("a/x", "b/y"),
     "cache lost, 60k rebilled, model changed"),
    (_short_row(evaluable=False, cause="start"), None, "cache unknown, first call"),
    (_short_row(evaluable=False, cause="usage_missing"), None, "cache unknown, usage unavailable, 1 min"),
    (_short_row(evaluable=False, cause="rewrite"), None, "cache unknown, context changed, 1 min"),
    (_short_row(evaluable=False, cause="auxiliary"), None, "cache unknown, background task, 1 min"),
    (_short_row(evaluable=False, api_mode="unknown"), None, "cache unknown, provider unknown, 1 min"),
    (_short_row(evaluable=False, cache_read_tokens=0), None, "cache unknown, too few cache hits, 1 min"),
    (_short_row(evaluable=False), None, "cache unknown, little history, 1 min"),
])
def test_outcome_text(env, row, change, text):
    assert env.plugin._outcome_text(row, change) == text


def test_warn_tag_is_short(env, monkeypatch):
    _guard_acts(env, monkeypatch, cached=0)
    env.plugin._WARNED["s1"] = True
    row = {"evaluable": True, "cold": True, "miss_delta": 23000, "gap_s": 600.0,
           "prompt_tokens": 60000, "cache_read_tokens": 0}
    env.plugin._PENDING.clear()
    env.plugin._queue_outcome({"session_id": "s1"}, True, row)
    assert env.plugin._PENDING["s1"] == "CGUARD WARN cache lost, 23k rebilled, 10 min"
