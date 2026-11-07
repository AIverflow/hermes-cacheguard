"""Tests of guard.py: the middleware, result lines, history cleaning, prefix hash."""
import json
import time
import types

import pytest
from conftest import (
    _cold_history,
    _run_counting,
    big_request,
    put_session,
    run_middleware,
    seed_session,
)

# ------------------------------------------------------------- formatting --


@pytest.fixture(autouse=True)
def _fixed_clock(env, monkeypatch):
    monkeypatch.setattr(env.plugin, "_clock", lambda: "13:05")


def test_warning_shows_nothing_before_the_reply(env):
    seed_session(env, ago=3600)
    run_middleware(env)
    assert not env.notices and env.warned() is not None


# -------------------------------------------------------------- idle trigger


def test_idle_trigger_warns_and_sends(env):
    seed_session(env, ago=3600)
    out, called, sentinel = run_middleware(env)
    assert called and out is sentinel
    assert not env.notices and env.warned() is not None


def test_gateway_default_off_still_records_usage(env):
    seed_session(env, ago=3600, model="p/m1", api_mode="chat_completions")
    out, called, sentinel = run_middleware(env, platform="telegram")
    assert called and out is sentinel and env.warned() is None
    assert env.plugin._load_observations()


@pytest.mark.parametrize("platform,setting", [("cli", "cli"), ("telegram", "gateway")])
@pytest.mark.parametrize("mode", ["off", "warn", "block"])
def test_surface_modes_gate_idle_trigger(env, platform, setting, mode):
    # The middleware never blocks: block is the pre_llm_call hook's job, so it only warns here.
    env.config.update({setting: mode, "size_k": 1})
    seed_session(env, ago=3600)
    out, called, sentinel = run_middleware(env, platform=platform)
    assert called and out is sentinel
    if mode == "off":
        assert env.warned() is None
    else:
        assert env.warned() is not None


@pytest.mark.parametrize("mode", ["warn", "block"])
def test_bot_chat_like_quiet_call_is_pure_pass_through(env, mode):
    # Bot Chat runs as `-c NAME -Q`: minted task id, so it is a background call.
    env.config.update(cli=mode, gateway=mode, size_k=1)
    seed_session(env, ago=3600)
    before = env.plugin._read_session("s1")
    out, called, sentinel = run_middleware(env, task_id=MINTED, model="new")
    assert called and out is sentinel
    assert env.warned() is None and "s1" not in env.plugin._PENDING
    assert env.plugin._read_session("s1") == before


@pytest.mark.parametrize("trigger", ["idle", "model_change"])
def test_gateway_warn_mode_is_nonblocking(env, trigger):
    env.config.update(gateway="warn", cli="off", size_k=1)
    if trigger == "idle":
        seed_session(env, ago=3600)
    else:
        seed_session(env, ago=10, model="p/old")
    out, called, sentinel = run_middleware(env, platform="telegram", model="new")
    assert called and out is sentinel and env.warned() is not None


MINTED = "3f2b8c1e-7a4d-4e6b-9c1a-0d5e2f8a1b34"  # Hermes mints a uuid4 when run_conversation gets no task_id


def _resume_quiet(env, task_id=MINTED, **extra):
    # `hermes chat -c NAME -Q` / `hermes -z -c NAME`: resumed session, minted task id.
    env.config.update(size_k=1)
    seed_session(env, ago=3600)
    extra.setdefault("api_request_id", f"s1:{task_id}:abcd1234:api:1")
    return run_middleware(env, task_id=task_id, **extra)


def test_quiet_resume_with_minted_task_id_passes_through(env):
    # One-shot runs (`-Q`, `-z`) get a minted task id: they count as background calls.
    out, called, sentinel = _resume_quiet(env)
    assert called and out is sentinel and env.warned() is None
    assert env.plugin._load_observations() == []  # background calls leave no row


@pytest.mark.parametrize("task", ["bg_101500_ab12cd", "subagent-0-1a2b3c4d"])
def test_named_task_id_stays_auxiliary(env, task):
    out, called, sentinel = _resume_quiet(env, task_id=task)
    assert called and out is sentinel and env.warned() is None


@pytest.mark.parametrize("trigger", ["idle", "model_change"])
def test_surface_off_cannot_be_reenabled_by_model_change(env, trigger):
    env.config.update(gateway="off", cli="block")
    if trigger == "idle":
        seed_session(env, ago=3600)
    else:
        seed_session(env, ago=10, model="p/old")
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


# ----------------------------------------------------------- model change --


@pytest.mark.parametrize("mode", ["off", "warn", "block"])
def test_model_change_off_warn_block(env, mode):
    seed_session(env, ago=10, model="p/old")  # short gap: only model-change can fire
    env.config["cli"] = mode
    out, called, sentinel = run_middleware(env, model="new")
    assert called and out is sentinel  # block is the pre_llm_call hook's job: here it only warns
    if mode == "off":
        assert env.warned() is None
    else:
        assert "model changed" in env.warned()


def test_model_change_same_model_silent(env):
    seed_session(env, ago=10, model="p/m1")
    run_middleware(env, model="m1")
    assert env.warned() is None


def test_model_change_small_context_silent(env):
    seed_session(env, ago=10, model="p/old")
    run_middleware(env, model="new", request={"messages": [], "tools": []})
    assert env.warned() is None


# ------------------------------------------------------- plugin errors --


def test_error_in_the_check_sends_once(env, monkeypatch):
    env.config["cli"] = "block"
    seed_session(env, ago=3600)
    monkeypatch.setattr(env.plugin, "_evaluate", lambda *a, **k: 1 / 0)
    out, calls, sentinel = _run_counting(env)
    assert calls == 1 and out is sentinel


def test_error_in_the_mode_action_sends_once(env, monkeypatch):
    env.config["cli"] = "warn"
    seed_session(env, ago=3600)
    monkeypatch.setattr(env.plugin, "_act", lambda *a, **k: 1 / 0)
    out, calls, sentinel = _run_counting(env)
    assert calls == 1 and out is sentinel


# ------------------------------------------------- own session file only --


def test_unknown_session_means_no_warning_even_when_hermes_has_history(env):
    con = env.db()
    con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES ('s1','assistant','a',?)",
                (time.time() - 7200,))
    con.execute("INSERT INTO session_model_usage VALUES ('s1','','deepinfra','old',1.0)")
    con.commit()
    con.close()
    env.config.update(cli="block", size_k=1)
    out, called, sentinel = run_middleware(env, model="new", provider="p")
    assert called and out is sentinel and env.warned() is None


@pytest.mark.parametrize("content", ["not json", "[1, 2]", ""])
def test_broken_session_file_means_no_warning(env, content):
    env.config.update(cli="block", size_k=1)
    env.plugin._session_file("s1").write_text(content)
    out, called, sentinel = run_middleware(env, model="new", provider="p")
    assert called and out is sentinel and env.warned() is None


def _usage(prompt, cached):
    return {"prompt_tokens": prompt, "cache_read_tokens": cached}


def _send_observed(env, **kwargs):
    from types import SimpleNamespace
    response = SimpleNamespace(usage=kwargs.pop("usage", None))
    kwargs.update(next_call=lambda: response, request={}, api_call_count=2, api_mode="chat_completions")
    return env.plugin._on_llm_execution(**kwargs)


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


def test_last_time_in_the_future_means_no_warning(env):
    env.config["cli"] = "block"
    seed_session(env, ago=-86400)  # clock went back: stored time is a day ahead, the pause is unknown
    out, called, sentinel = run_middleware(env)
    assert called and out is sentinel


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


def _turn(env, monkeypatch, *, mode="warn", cached=59000, model="m1"):
    """Baseline call, then a call 60 minutes later on the same session. Returns the provider calls."""
    from types import SimpleNamespace
    env.config["cli"] = mode
    calls = []
    now = [100.0]
    monkeypatch.setattr(env.plugin.time, "time", lambda: now[0])
    _send_observed(env, session_id="s1", model="m1", provider="p", usage=_usage(60000, 59000))
    now[0] = 3700.0
    response = SimpleNamespace(usage=_usage(60000, cached))
    env.plugin._on_llm_execution(next_call=lambda: (calls.append(1), response)[1], request=big_request(),
                                 session_id="s1", model=model, provider="p", api_call_count=1,
                                 api_mode="chat_completions", platform="cli")
    return calls


def test_kept_line_hidden_when_option_unset(env, monkeypatch):
    assert "display_on_success" not in env.config
    calls = _turn(env, monkeypatch, mode="warn")
    assert calls == [1]
    assert env.plugin._PENDING == {} and _reply(env) is None


def test_kept_line_shown_when_option_on_warn(env, monkeypatch):
    env.config["display_on_success"] = True
    _turn(env, monkeypatch, mode="warn")
    assert _reply(env) is None and env.posted == [RESULT_LINE]


def test_loss_line_shown_when_option_off(env, monkeypatch):
    env.config["display_on_success"] = False
    _turn(env, monkeypatch, mode="warn", cached=1000)
    assert _reply(env) is None
    assert env.posted == ["CGUARD 13:05 WARN cache lost, 59k rebilled, 60 min"]


def test_unknown_line_shown_when_option_off(env, monkeypatch):
    env.config["display_on_success"] = False
    _turn(env, monkeypatch, mode="warn", cached=0)
    assert _reply(env) is None
    assert env.posted == ["CGUARD 13:05 WARN cache unknown, needs 2 more cache hits, 60 min"]


def test_model_changed_line_hidden_when_cache_kept_and_option_off(env, monkeypatch):
    env.config["display_on_success"] = False
    _turn(env, monkeypatch, mode="warn", model="m2")
    assert env.plugin._PENDING == {} and _reply(env) is None


def test_model_changed_line_shown_when_cache_kept_and_option_on(env, monkeypatch):
    env.config["display_on_success"] = True
    _turn(env, monkeypatch, mode="warn", model="m2")
    assert _reply(env) is None and env.posted == ["CGUARD 13:05 OK 59k reused, model changed"]


def test_model_changed_line_shown_when_cache_lost_whatever_the_option(env, monkeypatch):
    env.config["display_on_success"] = False
    _turn(env, monkeypatch, mode="warn", model="m2", cached=0)
    assert _reply(env) is None
    assert env.posted == ["CGUARD 13:05 WARN cache lost, 60k rebilled, model changed"]


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
    assert env.posted == ["CGUARD 13:05 WARN cache lost, 59k rebilled, 60 min"]


def test_zero_read_with_one_positive_reports_no_baseline_yet(env, monkeypatch):
    _guard_acts(env, monkeypatch, cached=0)
    _reply(env)
    assert env.posted == ["CGUARD 13:05 WARN cache unknown, needs 2 more cache hits, 60 min"]


def test_outcome_without_finite_pause_omits_clause(env):
    row = {"evaluable": True, "cold": True, "miss_delta": 5, "gap_s": None}
    assert env.plugin._outcome_text(row) == "cache lost, 5 rebilled"


def test_gateway_outcome_uses_the_same_label_as_the_cli(env, monkeypatch):
    env.config.update(gateway="warn")
    _guard_acts(env, monkeypatch, platform="telegram")
    assert _reply(env) is None
    assert env.posted == [RESULT_LINE]


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


# ------------------------------------------------- final message format


def test_kept_result_line_leaves_out_the_estimate_shown_before_sending(env, monkeypatch):
    env.config.update(cli="warn")
    _cold_history(env.plugin, cold=6, warm=4, gap=3600)
    _guard_acts(env, monkeypatch)
    _reply(env)
    assert env.posted == [RESULT_LINE]


def test_model_change_result_has_no_pause_or_estimate(env):
    row = {"evaluable": False, "cause": "model_switch", "gap_s": 3600.0, "prompt_tokens": 30000,
           "cache_read_tokens": 0}
    assert env.plugin._outcome_text(row, ("p/old", "p/new")) == "cache lost, 30k rebilled, model changed"


# ------------------------------------------------------- surface groups ---


@pytest.mark.parametrize("platform", ["cli", "tui", "local", "desktop", "webui", "acp"])
def test_cli_group_platforms_use_cli_setting(env, platform):
    env.config.update(cli="warn", gateway="off", size_k=1)
    seed_session(env, ago=3600)
    out, called, sentinel = run_middleware(env, platform=platform)
    assert called and out is sentinel and env.warned() is not None


@pytest.mark.parametrize("platform", ["telegram", "discord", "some-new-chat-app"])
def test_messaging_and_unknown_platforms_use_gateway_setting(env, platform):
    env.config.update(cli="block", gateway="warn", size_k=1)
    seed_session(env, ago=3600)
    out, called, sentinel = run_middleware(env, platform=platform)
    assert called and out is sentinel and env.warned() is not None


@pytest.mark.parametrize("platform", ["cron", "kanban", "subagent", "curator", "api_server", "webhook", "msgraph_webhook"])
@pytest.mark.parametrize("mode", ["warn", "block"])
def test_unattended_platforms_are_pure_pass_through(env, platform, mode):
    env.config.update(cli=mode, gateway=mode, size_k=1)
    seed_session(env, ago=3600)
    before = env.plugin._read_session("s1")
    out, called, sentinel = run_middleware(env, platform=platform, model="new")
    assert called and out is sentinel
    assert env.warned() is None and "s1" not in env.plugin._PENDING
    assert env.plugin._read_observations() == [] and env.plugin._read_session("s1") == before
    env.plugin._PENDING["s1"] = "[CACHEGUARD] stale"
    assert env.plugin._transform_llm_output(
        response_text="hi", session_id="s1", platform=platform) is None


def _kanban_worker(env, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_42")


def _cron_run(env, monkeypatch):
    env.cron_run = types.SimpleNamespace(job_id="j1")


@pytest.mark.parametrize("mark", [_kanban_worker, _cron_run])
def test_kanban_and_cron_workers_on_cli_are_left_alone(env, monkeypatch, mark):
    mark(env, monkeypatch)
    env.config.update(cli="block", size_k=1)
    seed_session(env, ago=3600)
    out, called, sentinel = run_middleware(env, platform="cli", model="new")
    assert called and out is sentinel
    assert env.warned() is None and env.plugin._read_observations() == []


def _blocked(p):
    return p._block_text(60000, "p/m1", None, 700.0, "s1")


def _req(p, msgs, key="messages"):
    return p._on_llm_request(request={"model": "m", key: msgs}, session_id="s1", platform="cli")


# ------------------------------------------- llm_request: lines kept out of the model's view --


def test_request_blocked_pair_removed_and_normal_pairs_kept(env):
    p = env.plugin
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "A"},
            {"role": "user", "content": HELD}, {"role": "assistant", "content": _blocked(p)},
            {"role": "user", "content": "b"}, {"role": "assistant", "content": "B"}]
    out = _req(p, msgs)["request"]["messages"]
    assert [m["content"] for m in out] == ["a", "A", "b", "B"]


def test_request_blocked_looking_reply_after_a_real_question_is_kept(env):
    """A model can write the BLOCKED text itself: it is ours only right after Hermes's withheld row."""
    p = env.plugin
    msgs = [{"role": "user", "content": "what does the block say?"}, {"role": "assistant", "content": _blocked(p)},
            {"role": "user", "content": "thanks"}]
    assert _req(p, msgs) is None


def test_request_blocked_looking_reply_at_the_start_is_kept(env):
    p = env.plugin
    msgs = [{"role": "assistant", "content": _blocked(p)}, {"role": "user", "content": "b"}]
    assert _req(p, msgs) is None


def test_request_is_idempotent_and_prefix_stable(env):
    p = env.plugin
    h1 = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "A"},
          {"role": "user", "content": HELD}, {"role": "assistant", "content": _blocked(p)}]
    h2 = h1 + [{"role": "user", "content": "c"}, {"role": "assistant", "content": "C"}]
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
    msgs = [{"role": "assistant", "content": _blocked(p)}]
    assert p._on_llm_request(request={"messages": msgs}, session_id="s1", platform="cron") is None


def test_request_part_lists_chat_and_anthropic(env):
    p = env.plugin
    def parts(t):
        return [{"type": "text", "text": t}]
    msgs = [{"role": "user", "content": parts("a")}, {"role": "assistant", "content": parts("A")},
            {"role": "user", "content": parts(HELD)}, {"role": "assistant", "content": parts(_blocked(p))}]
    out = _req(p, msgs)["request"]["messages"]
    assert out == [msgs[0], msgs[1]]


def test_request_codex_input_shapes(env):
    p = env.plugin
    def ot(t):
        return [{"type": "output_text", "text": t}]
    items = [{"role": "user", "content": "a"},
             {"type": "message", "role": "assistant", "status": "completed", "content": ot("A")},
             {"role": "user", "content": [{"type": "input_text", "text": HELD}]},
             {"type": "message", "role": "assistant", "content": ot(_blocked(p))},
             {"type": "function_call_output", "call_id": "c", "output": "x"}]
    out = _req(p, items, key="input")["request"]["input"]
    assert out == [items[0], items[1], items[4]]


def test_request_tool_result_before_blocked_is_kept(env):
    p = env.plugin
    msgs = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "r"}]},
            {"role": "user", "content": HELD}, {"role": "assistant", "content": _blocked(p)}]
    out = _req(p, msgs)["request"]["messages"]
    assert len(out) == 1 and out[0]["role"] == "user"


RESULT_LINE = "CGUARD 13:05 OK 59k reused, 60 min"


# --- notices: posted outside the model context -------------------------------- #


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
    for i in range(env.plugin._CAP + 10):
        _reply(env, session_id=f"x{i}")
    assert len(env.plugin._NOTICES_SEEN) == env.plugin._CAP


def test_unattended_posts_nothing(env, monkeypatch):
    _guard_acts(env, monkeypatch)
    env.plugin._notice("something broke")
    out = env.plugin._transform_llm_output(
        response_text="hi", session_id="s1", platform="cron", turn_id="t1")
    assert out is None and env.posted == [] and "s1" in env.plugin._PENDING


# --- short posted lines ------------------------------------------------------- #


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
    (_short_row(evaluable=False, api_mode="unknown"), None, "cache unknown, provider unknown, 1 min"),
    (_short_row(evaluable=False, cache_read_tokens=0, needs_hits=2), None, "cache unknown, needs 2 more cache hits, 1 min"),
    (_short_row(evaluable=False, cache_read_tokens=0, needs_hits=1), None, "cache unknown, needs 1 more cache hit, 1 min"),
    (_short_row(evaluable=False, cache_read_tokens=0), None, "cache unknown, little history, 1 min"),
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
    env.plugin._queue_outcome({"session_id": "s1"}, row)
    assert env.plugin._PENDING["s1"] == "CGUARD 13:05 WARN cache lost, 23k rebilled, 10 min"


def _msgs(n, mid="middle"):
    out = [{"role": "system", "content": "You are helpful."}]
    for i in range(n):
        out.append({"role": "user" if i % 2 == 0 else "assistant",
                    "content": (mid if i == 3 else f"message {i} " + "x" * 400)})
    return out


def _call(env, msgs, n, session_id="s", prompt=1000, cached=1000, **extra):
    """One call: the llm_request hook, then the answer is recorded. Returns the observation row."""
    p = env.plugin
    ctx = dict(session_id=session_id, platform="cli", provider="p", model="m1", api_mode="chat_completions",
               api_request_id=f"{session_id}:{session_id}:api:{n}", task_id=session_id, **extra)
    p._on_llm_request(request={"model": "m1", "messages": msgs}, **ctx)
    return p._record_response(ctx, {"usage": {"prompt_tokens": prompt, "cache_read_tokens": cached}},
                              1000.0 * n - 1, 1000.0 * n, True)


def _rec(env, session_id="s"):
    return env.plugin._get_session(session_id)


def test_same_prefix_and_new_message_is_not_marked(env):
    h = _msgs(6)
    assert _call(env, h, 1)["cause"] == "start"
    row = _call(env, h + [{"role": "user", "content": "next"}], 2)
    assert row["cause"] is None and row["cache_read_tokens"] == 1000


def test_hash_is_saved_in_the_session_file_next_to_the_rest(env):
    h = _msgs(6)
    _call(env, h, 1)
    rec = _rec(env)
    assert rec["prefix_n"] == len(h) and len(rec["prefix_hash"]) == 64
    assert rec["model"] == "p/m1" and rec["prompt_tokens"] == 1000  # the rest of the record is kept
    assert "x" * 50 not in json.dumps(rec)  # a fingerprint, not the chat


def test_small_edit_in_the_middle_is_context_changed(env):
    h = _msgs(8)
    _call(env, h, 1)
    edited = _msgs(8, mid="middle!")  # a few bytes out of thousands: far below 5 %
    row = _call(env, edited + [{"role": "user", "content": "next"}], 2, prompt=1000, cached=0)
    assert row["cause"] == "rewrite"
    verdict = env.plugin.evaluate_observations([row])[0]
    assert verdict["evaluable"] is False and verdict["cold"] is None
    assert "context changed" in env.plugin._outcome_text(verdict)


def test_next_call_after_a_change_is_judged_again(env):
    _call(env, _msgs(8), 1)
    edited = _msgs(8, mid="other")
    assert _call(env, edited, 2)["cause"] == "rewrite"
    assert _call(env, edited + [{"role": "user", "content": "go"}], 3)["cause"] is None


def test_leftover_cacheguard_line_in_the_prefix_is_context_changed(env):
    p = env.plugin
    h = _msgs(8)
    _call(env, h, 1)
    # The same chat, but an old user message now carries one of our lines (the cleaner only cuts replies).
    dirty = [dict(m) for m in h]
    dirty[3] = {**h[3], "content": h[3]["content"] + "\n" + p._PREFIX + " leftover"}
    assert p._clean_history(dirty)[3] == dirty[3]
    row = _call(env, dirty + [{"role": "user", "content": "next"}], 2, prompt=1000, cached=900)
    assert row["cause"] == "rewrite"


def test_cache_markers_moving_between_calls_are_not_a_change(env):
    def marked(msgs, upto):
        out = [dict(m) for m in msgs]
        for i in range(upto, len(out)):  # the marker window: a string becomes a one-part list
            out[i] = {**out[i], "content": [{"type": "text", "text": out[i]["content"],
                                              "cache_control": {"type": "ephemeral"}}]}
        return out
    h = _msgs(8)
    _call(env, marked(h, 6), 1)
    row = _call(env, marked(h + [{"role": "user", "content": "n"}], 8), 2)
    assert row["cause"] is None


def test_fewer_messages_than_before_is_a_change(env):
    h = _msgs(8)
    _call(env, h, 1)
    assert _call(env, h[:4], 2)["cause"] == "rewrite"


def test_old_5_percent_rule_still_works(env):
    h = _msgs(6)
    _call(env, h, 1, prompt=1000)
    assert _call(env, h + [{"role": "user", "content": "n"}], 2, prompt=800, cached=800)["cause"] == "rewrite"


def test_hash_error_gives_todays_result(env):
    h = _msgs(8)
    _call(env, h, 1)
    bad = _msgs(8, mid="changed") + [{"role": "user", "content": object()}]  # not JSON data
    row = _call(env, bad, 2, prompt=1000, cached=0)
    assert row["cause"] is None  # no mark: judged as before (read 0 is a plain miss)
    assert _rec(env)["prefix_hash"] == env.plugin._hash_messages(h, 0)[1]  # the old hash is kept


def test_hash_function_crash_does_not_touch_the_request(env, monkeypatch):
    p = env.plugin
    monkeypatch.setattr(p, "_hash_messages", lambda *a: 1 / 0)
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "A"},
            {"role": "user", "content": HELD}, {"role": "assistant", "content": p._block_text(60000, "p/m1", None, 700.0, "s")}]
    out = p._on_llm_request(request={"messages": msgs}, session_id="s", platform="cli")
    assert out["request"]["messages"] == msgs[:2]  # still cleaned
    assert _call(env, _msgs(4), 1)["cause"] == "start"


def test_broken_session_file_gives_todays_result(env):
    h = _msgs(8)
    _call(env, h, 1)
    env.plugin._session_file("s").write_text("{not json", encoding="utf-8")
    assert _call(env, _msgs(8, mid="changed"), 2)["cause"] == "start"  # no baseline at all, as before
    # the file is whole again afterwards, with a new hash
    assert _rec(env)["prefix_n"] == 9


def test_unreadable_hash_fields_are_ignored(env):
    h = _msgs(8)
    _call(env, h, 1)
    rec = _rec(env)
    put_session(env.plugin, "s", dict(rec, prefix_n="many", prefix_hash=5))
    assert _call(env, _msgs(8, mid="changed"), 2)["cause"] is None
    put_session(env.plugin, "s", dict(rec, prefix_n=True, prefix_hash=rec["prefix_hash"]))
    assert _call(env, _msgs(8, mid="again"), 3)["cause"] is None


def test_new_session_id_has_no_hash_and_starts_clean(env):
    h = _msgs(8)
    _call(env, h, 1, session_id="old")
    row = _call(env, _msgs(8, mid="summary"), 1, session_id="new")
    assert row["cause"] == "start"
    assert _rec(env, "old")["prefix_n"] == len(h)  # the old chat is untouched


def test_background_calls_neither_read_nor_move_the_hash(env):
    h = _msgs(8)
    _call(env, h, 1)
    before = _rec(env)
    p = env.plugin
    aux = dict(session_id="s", platform="cli", provider="p", model="m1", api_mode="chat_completions",
               api_request_id="s:other:api:1", task_id="other")
    p._on_llm_request(request={"messages": [{"role": "user", "content": "title?"}]}, **aux)
    p._record_response(aux, {"usage": {"prompt_tokens": 50, "cache_read_tokens": 0}}, 1500.0, 1501.0, False)
    after = _rec(env)
    assert (after["prefix_n"], after["prefix_hash"]) == (before["prefix_n"], before["prefix_hash"])
    assert _call(env, h + [{"role": "user", "content": "n"}], 2)["cause"] is None


def test_background_call_does_not_read_nor_change_the_pending_hash(env, monkeypatch):
    _call(env, _msgs(8), 1)
    p = env.plugin
    monkeypatch.setattr(p, "_get_session", lambda sid: 1 / 0)  # a read would raise
    before = dict(p._PREFIX_PENDING)
    p._note_prefix({"session_id": "s", "task_id": "other", "api_request_id": "r"}, _msgs(8, mid="aux"))
    assert before == p._PREFIX_PENDING


def test_call_without_a_session_id_notes_no_hash(env):
    env.plugin._note_prefix({"api_request_id": "r"}, _msgs(4))
    env.plugin._note_prefix({"session_id": "", "api_request_id": "r"}, _msgs(4))
    assert env.plugin._PREFIX_PENDING == {}


def test_failed_provider_call_does_not_move_the_hash(env):
    p = env.plugin
    h = _msgs(8)
    _call(env, h, 1)
    ctx = dict(session_id="s", platform="cli", provider="p", model="m1", api_mode="chat_completions",
               api_request_id="s:s:api:2", task_id="s")
    p._on_llm_request(request={"messages": _msgs(8, mid="never answered")}, **ctx)  # no answer recorded
    assert _rec(env)["prefix_hash"] == p._hash_messages(h, 0)[1]
    # the retry still sees the change against the last answered call
    assert _call(env, _msgs(8, mid="never answered"), 3)["cause"] == "rewrite"


def test_unattended_calls_store_nothing(env):
    p = env.plugin
    p._on_llm_request(request={"messages": _msgs(4)}, session_id="s", platform="cron", api_request_id="r")
    assert p._PREFIX_PENDING == {}


def test_codex_input_list_is_hashed_too(env):
    p = env.plugin
    ctx = dict(session_id="s", platform="cli", provider="p", model="m1", api_mode="codex_responses",
               api_request_id="s:s:api:1", task_id="s")
    p._on_llm_request(request={"input": _msgs(4)}, **ctx)
    p._record_response(ctx, {"usage": {"prompt_tokens": 10, "cache_read_tokens": 0}}, 1.0, 2.0, False)
    assert _rec(env)["prefix_n"] == 5


def test_hash_is_stable_and_order_of_keys_does_not_matter(env):
    p = env.plugin
    a = [{"role": "user", "content": "é", "name": "n"}]
    b = [{"name": "n", "content": "é", "role": "user"}]
    assert p._hash_messages(a, 1) == p._hash_messages(b, 1)
    assert p._hash_messages(a, 1)[0] == p._hash_messages(a + [{"role": "user", "content": "x"}], 1)[0]
    assert p._hash_messages(a, 1)[1] != p._hash_messages([{"role": "user", "content": "e", "name": "n"}], 1)[1]


def test_bedrock_cache_point_moving_between_calls_is_not_a_change(env):
    def converse(n, marked):  # Hermes adds a cachePoint block to the second-to-last message only
        out = [{"role": "user" if i % 2 == 0 else "assistant", "content": [{"text": f"m{i}"}]} for i in range(n)]
        out[marked]["content"].append({"cachePoint": {"type": "default"}})
        return out
    _call(env, converse(4, 2), 1)
    assert _call(env, converse(6, 4), 2)["cause"] is None


def _history(p):
    """A realistic chat: text and list contents, a tool call, a BLOCKED pair, notes on replies."""
    note = "\n\n" + p._PREFIX + " result: 5k reused"
    blocked = p._block_text(60000, "p/m1", None, 700.0, "s1")
    return [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Plan the trip."},
        {"role": "assistant", "content": "Sure.\n\n- day 1\n- day 2" + note},
        {"role": "user", "content": [{"type": "text", "text": "Now list hotels"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Hotels:\n1. Ace\n2. Bay" + note}]},
        {"role": "user", "content": "Book the cheapest one."},
        {"role": "user", "content": p._WITHHELD},
        {"role": "assistant", "content": blocked},
        {"role": "user", "content": "Book the cheapest one."},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result: 3 hotels\nprice: 80"},
        {"role": "assistant", "content": "Booked Ace." + note},
    ]


def _dump(items):
    return json.dumps(items, sort_keys=True)


def test_cleaning_twice_equals_cleaning_once(env):
    p = env.plugin
    raw = _history(p)
    once = p._clean_history(raw)
    assert once != raw  # the fixture really has lines to remove
    assert _dump(p._clean_history(once)) == _dump(once)


def test_cleaning_equal_inputs_gives_identical_bytes(env):
    p = env.plugin
    raw = _history(p)
    before = _dump(raw)
    first = _dump(p._clean_history(raw))
    assert _dump(p._clean_history(raw)) == first
    assert _dump(p._clean_history(_history(p))) == first
    assert _dump(raw) == before  # cleaning does not change the history it was given


def test_growing_history_keeps_identical_cleaned_prefix(env):
    p = env.plugin
    full = _history(p) + [
        {"role": "user", "content": "Thanks"},
        {"role": "assistant", "content": "Welcome." + "\n\n" + p._PREFIX + " result: 2k reused"},
        {"role": "user", "content": "Bye"},
        {"role": "user", "content": p._WITHHELD},
        {"role": "assistant", "content": p._block_text(60000, "p/m1", None, 900.0, "s1")},
        {"role": "user", "content": "Bye"},
        {"role": "assistant", "content": "Bye!"},
    ]
    whole = p._clean_history(full)
    checked = 0
    # Cut only after an assistant reply: a BLOCKED pair removes the user message before it,
    # so a cut right after a user message is not a prefix the provider ever saw.
    for k in range(1, len(full) + 1):
        if full[k - 1]["role"] != "assistant":
            continue
        short = p._clean_history(full[:k])
        assert _dump(whole[:len(short)]) == _dump(short)
        checked += 1
    assert checked >= 5


def test_every_provider_return_is_recorded_before_intake_even_without_usage(env, monkeypatch):
    from types import SimpleNamespace
    p = env.plugin
    put_session(p, 's', {'last_api_ts': 1.0, 'model': 'p/m', 'api_mode': 'chat_completions'})
    now = [1000.0]
    monkeypatch.setattr(p.time, 'time', lambda: now[0])
    for i, response in enumerate((SimpleNamespace(usage=None, refusal=True),
                                  {'usage': None, 'finish_reason': 'length'}, object())):
        now[0] += 1000
        result = p._on_llm_execution(next_call=lambda response=response: response, request={}, session_id='s',
                                    model='m', provider='p', api_mode='chat_completions',
                                    api_request_id=str(i), api_call_count=2)
        assert result is response
    rows = p._load_observations()
    assert len(rows) == 3 and all(r['cause'] == 'usage_missing' for r in rows)
    assert all(r['prompt_tokens'] is None and r['cache_read_tokens'] is None for r in rows)


def install_normalizer(monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace
    calls = []
    def normalize(raw, *, provider, api_mode):
        calls.append((raw, provider, api_mode))
        return SimpleNamespace(prompt_tokens=raw.get('prompt_tokens', 0),
                               cache_read_tokens=raw.get('cached', 0),
                               cache_write_tokens=raw.get('written', 0))
    mod = ModuleType('agent.usage_pricing')
    mod.normalize_usage = normalize
    monkeypatch.setitem(sys.modules, 'agent.usage_pricing', mod)
    return calls


def send(env, usage=None, **kw):
    from types import SimpleNamespace
    response = SimpleNamespace(usage=usage)
    payload = dict(request={}, session_id='s', model='m', provider='p', api_mode='chat_completions',
                   api_call_count=1, api_request_id='r')
    payload.update(kw)
    return env.plugin._on_llm_execution(next_call=lambda: response, **payload)


@pytest.mark.parametrize('task', ['background_review', 'security', 'memory', 'skills'])
def test_background_return_does_not_replace_main_baseline_or_timer(env, monkeypatch, task):
    install_normalizer(monkeypatch)
    p = env.plugin
    now = [100.0]
    monkeypatch.setattr(p.time, 'time', lambda: now[0])
    send(env, {'prompt_tokens': 18731, 'cached': 11776}, task_id='s')
    before = p._get_session('s')
    now[0] = 110.0
    send(env, {'prompt_tokens': 20944, 'cached': 18560}, task_id=task,
         api_request_id=f's:{task}:turn:api:1')
    after = p._get_session('s')
    assert after.pop('cache_touch_ts') == 110.0 and after == before
    assert p._load_observations() == []  # the background call left no row
    now[0] = 200.0
    send(env, {'prompt_tokens': 18752, 'cached': 18560}, task_id='s')
    rows = p._load_observations()
    row = rows[-1]
    assert len(rows) == 1 and row['gap_s'] == 100.0 and row['prev_prompt_tokens'] == 18731
    verdict = p.evaluate_observations(rows)[-1]
    assert row['cause'] is None and verdict['evaluable'] and not verdict['cold']


@pytest.mark.parametrize('cached, model, warned', [
    (40000, 'm', False),   # background call read most of the chat: cache kept warm
    (640, 'm', True),      # read only a small piece: no proof the chat stayed cached
    (40000, 'other', True),  # another model's cache
])
def test_background_cache_read_restarts_guard_idle_clock(env, monkeypatch, cached, model, warned):
    install_normalizer(monkeypatch)
    p = env.plugin
    env.settings.update(cli='warn', display_on_success=True)
    now = [100.0]
    monkeypatch.setattr(p.time, 'time', lambda: now[0])
    send(env, {'prompt_tokens': 50000, 'cached': 49000}, task_id='s')
    now[0] = 100.0 + 1800
    send(env, {'prompt_tokens': 52000, 'cached': cached}, task_id='review', model=model)
    now[0] = 100.0 + 1860
    send(env, {'prompt_tokens': 50500, 'cached': 50000}, task_id='s')
    assert (env.warned('s') is not None) is warned
    assert p._load_observations()[-1]['gap_s'] == 1860.0  # stats still measure main-call pauses


def test_background_calls_never_show_main_guard(env, monkeypatch):
    install_normalizer(monkeypatch)
    env.settings.update(cli='block')
    seed_session(env, ago=3600, prompt_tokens=60000)
    before = env.plugin._get_session('s1')
    send(env, {'prompt_tokens': 61000, 'cached': 0}, session_id='s1', task_id='security',
         model='other-model', request=big_request(), api_request_id='s1:security:turn:api:1')
    assert env.plugin._get_session('s1') == before
    assert not env.notices


def test_background_returns_without_usage_and_concurrent_returns_are_preserved(env, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    install_normalizer(monkeypatch)
    p = env.plugin
    send(env, {'prompt_tokens':18731, 'cached':11776}, task_id='s')
    before = p._get_session('s')
    send(env, None, task_id='memory')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: send(env, {'prompt_tokens':21000+i,'cached':19000},
                                    task_id=f'worker-{i}',api_request_id=f'worker-{i}'),range(12)))
    assert p._load_observations() == []  # no background call leaves a row
    after = p._get_session('s')
    after.pop('cache_touch_ts')  # the workers read most of the chat
    assert after == before


def test_main_task_identity_wins_over_arbitrary_or_conflicting_request_id(env, monkeypatch):
    install_normalizer(monkeypatch)
    send(env, {'prompt_tokens':100,'cached':90}, task_id='s',
         api_request_id='s:worker:turn:api:1')
    # The recorder trusts the explicit current task identity, not an ID inference.
    assert env.plugin._get_session('s')['prompt_tokens'] == 100


def test_present_empty_usage_stays_zero_and_missing_usage_stays_null(env, monkeypatch):
    calls = install_normalizer(monkeypatch)
    p = env.plugin
    put_session(p, 's', {'last_api_ts': 1.0, 'model': 'p/m', 'api_mode': 'chat_completions'})
    now = [1000.0]
    monkeypatch.setattr(p.time, 'time', lambda: now[0])
    send(env, {})
    now[0] += 1000
    send(env, None)
    rows = env.plugin._load_observations()
    assert rows[0]['prompt_tokens'] == rows[0]['cache_read_tokens'] == rows[0]['cache_write_tokens'] == 0
    assert rows[1]['prompt_tokens'] is rows[1]['cache_read_tokens'] is rows[1]['cache_write_tokens'] is None
    assert len(calls) == 1


def test_post_call_storage_error_never_replays_provider(env, monkeypatch):
    install_normalizer(monkeypatch)
    p = env.plugin
    called = []
    response = {'usage': {'prompt_tokens': 5}}
    def fail(*a):
        raise OSError('disk failed')
    monkeypatch.setattr(p, '_write_observation', fail)
    result = p._on_llm_execution(next_call=lambda: (called.append(1), response)[1], request={},
                                session_id='s', model='m', provider='p', api_call_count=2)
    assert result is response and called == [1]


def test_error_recording_the_reply_still_returns_it_without_a_second_call(env, monkeypatch):
    seen = []
    monkeypatch.setattr(env.plugin, "_record_response", lambda *a: (seen.append(1), 1 / 0))
    out, calls, sentinel = _run_counting(env)
    assert seen == [1] and calls == 1 and out is sentinel


def test_error_before_the_send_still_sends_once(env, monkeypatch):
    seed_session(env, ago=3600)  # a pause: the call is guarded, so the send is remembered first
    seen = []
    monkeypatch.setattr(env.plugin, "_remember_send", lambda *a: (seen.append(1), 1 / 0))
    out, calls, sentinel = _run_counting(env)
    assert seen == [1] and calls == 1 and out is sentinel


def test_failed_attempt_is_not_recorded_and_retry_return_is_recorded(env, monkeypatch):
    import pytest
    install_normalizer(monkeypatch)
    p = env.plugin
    def fail():
        raise RuntimeError('provider failed')
    with pytest.raises(RuntimeError, match='provider failed'):
        p._on_llm_execution(next_call=fail, request={}, session_id='s', api_request_id='retry')
    assert p._load_observations() == []
    put_session(p, 's', {'last_api_ts': 1.0, 'model': 'p/m', 'api_mode': 'chat_completions'})
    send(env, {'prompt_tokens': 100}, api_request_id='retry')
    assert len(p._load_observations()) == 1


def test_concurrent_returns_have_complete_rows_and_atomic_session_state(env, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    install_normalizer(monkeypatch)
    for i in range(30):
        put_session(env.plugin, f's{i}', {'last_api_ts': 1.0, 'model': 'p/m', 'api_mode': 'chat_completions'})
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: send(env, {'prompt_tokens': 100 + i, 'cached': 90}, session_id=f's{i}',
                                    api_call_count=2, api_request_id=str(i)), range(30)))
    rows = env.plugin._load_observations()
    assert len(rows) == 30 and len({r['ts'] for r in rows}) > 1 and {r['prompt_tokens'] for r in rows} == set(range(100, 130))
    for i in range(30):
        rec = env.plugin._get_session(f's{i}')
        assert rec['prompt_tokens'] == 100 + i and rec['last_api_ts'] in {r['ts'] for r in rows}
    assert not list((env.home/'cacheguard/sessions').glob('*.tmp'))


def test_warn_label_only_when_cost_is_likely(env):
    p = env.plugin
    reused = {'evaluable': True, 'cold': False, 'cache_read_tokens': 59000, 'gap_s': 3600}
    cold = {'evaluable': True, 'cold': True, 'miss_delta': 59000, 'gap_s': 3600}
    for row, label in ((reused, 'OK '), (cold, 'WARN ')):
        p._WARNED['s'] = 'note'
        p._queue_outcome({'session_id': 's'}, row, display_on_success=True)
        assert label in p._PENDING.pop('s')


def test_zero_cached_without_baseline_says_so(env):
    row = {'evaluable': False, 'comparable': True, 'cause': None,
           'prompt_tokens': 50000, 'cache_read_tokens': 0, 'api_mode': 'chat_completions',
           'model': 'p/m', 'gap_s': 3600, 'needs_hits': 1}
    text = env.plugin._outcome_text(row)
    assert text == 'cache unknown, needs 1 more cache hit, 60 min'


def test_first_call_of_chat_says_it_is_normal(env):
    row = {'evaluable': False, 'cause': 'start', 'gap_s': None, 'prompt_tokens': 50000,
           'cache_read_tokens': 0, 'api_mode': 'chat_completions', 'model': 'p/m'}
    assert env.plugin._outcome_text(row) == 'cache unknown, first call'


# ------------------------------------------------- pre_llm_call: the block --

HELD = "[Message withheld: a pre_llm_call policy hook blocked it.]"
RETRY_LINE = "\nTo send it anyway: /retry, or send it again.\nOr start /new and paste it. Session ID: s1"
IMAGE_LINE = "\nTo send it anyway: attach the image again and send it again.\nOr start /new and paste it. Session ID: s1"


def _hook(env, text="x" * 240000, *, session_id="s1", model="m1", platform="cli", task_id=None, history=None,
          **extra):
    """Call the pre_llm_call hook the way Hermes does (bare model name, last history item = the message)."""
    return env.plugin._on_pre_llm_call(
        conversation_history=history if history is not None else [{"role": "user", "content": text}],
        user_message=text, session_id=session_id, task_id=task_id or session_id, model=model, platform=platform,
        is_first_turn=False, sender_id=None, turn_id="t1", parent_session_id=None, **extra)


def _blockable(env, mode="block", **kw):
    env.config["cli"] = mode
    seed_session(env, ago=3600, **kw)


def test_hook_blocks_after_a_pause_in_block_mode(env):
    _blockable(env)
    out = _hook(env)
    assert out == {"action": "block", "message": env.plugin._BLOCKED_PREFIX + "13:05 · 60 minutes pause 🕐 · ~60,008 tokens may be rebilled" + RETRY_LINE}


def test_hook_block_text_has_the_loss_counts_when_known(env):
    _blockable(env)
    _cold_history(env.plugin, cold=6, warm=4)
    out = _hook(env)
    assert "tokens may be rebilled · lost 6 of 10 similar pauses" + RETRY_LINE in out["message"]


def test_hook_block_text_says_one_minute_in_the_singular(env):
    env.config.update({"cli": "block", "pause_m": 1})
    seed_session(env, ago=70)
    assert "1 minute pause 🕐" in _hook(env)["message"]


def test_hook_uses_the_one_line_text_when_tiny_block_is_on(env):
    _blockable(env)
    env.config["tiny_block"] = True
    out = _hook(env)
    assert out == {"action": "block", "message": env.plugin._BLOCKED_PREFIX + "13:05 · 1h 🕐 · ~60kt"}
    assert env.plugin._is_blocked_reply({"role": "assistant", "content": out["message"]})


def test_hook_same_text_again_goes_through_once(env):
    _blockable(env)
    assert _hook(env)["action"] == "block"
    assert _hook(env) is None  # the resend: not evaluated again
    assert _hook(env)["action"] == "block"  # and the pass is used up


def test_hook_a_different_text_is_evaluated_again(env):
    _blockable(env)
    assert _hook(env)["action"] == "block"
    assert _hook(env, "y" * 240000)["action"] == "block"
    assert _hook(env, "y" * 240000) is None
    assert _hook(env, "small") is None  # a different small message: evaluated, nothing to say


def test_hook_resend_is_per_session(env):
    _blockable(env)
    seed_session(env, session_id="s2", ago=3600)
    assert _hook(env)["action"] == "block"
    assert _hook(env, session_id="s2")["action"] == "block"
    assert _hook(env) is None


@pytest.mark.parametrize("mode", ["off", "warn"])
def test_hook_never_blocks_in_off_or_warn_mode(env, mode):
    _blockable(env, mode)
    assert _hook(env) is None


def test_hook_uses_the_mode_of_the_platform_group(env):
    env.config.update({"cli": "warn", "gateway": "block"})
    seed_session(env, ago=3600)
    assert _hook(env) is None
    assert _hook(env, platform="telegram")["action"] == "block"


def test_hook_single_query_is_left_to_the_warning(env, monkeypatch):
    _blockable(env)
    monkeypatch.setattr(env.plugin, "_single_query", lambda: True)
    assert _hook(env) is None


@pytest.mark.parametrize("platform", ["cron", "kanban", "api_server"])
def test_hook_unattended_is_left_alone(env, platform):
    _blockable(env)
    assert _hook(env, platform=platform) is None


def test_hook_kanban_worker_on_cli_is_left_alone(env, monkeypatch):
    _blockable(env)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t1")
    assert _hook(env) is None


def test_hook_auxiliary_call_is_left_alone(env):
    _blockable(env)
    assert _hook(env, task_id="other") is None


@pytest.mark.parametrize("content", [
    [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}],
    [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}],
])
def test_hook_media_in_the_message_is_not_blocked(env, content):
    _blockable(env)
    assert _hook(env, history=[{"role": "user", "content": content}]) is None
    env.config["block_images"] = False
    assert _hook(env, history=[{"role": "user", "content": content}]) is None


def test_hook_block_images_blocks_a_message_with_an_image(env):
    _blockable(env)
    env.config["block_images"] = True
    content = [{"type": "text", "text": "x" * 240000},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]
    out = _hook(env, history=[{"role": "user", "content": content}])
    assert out["action"] == "block"
    assert out["message"].endswith(IMAGE_LINE) and "/retry" not in out["message"]


def test_hook_block_images_blocks_an_image_passed_as_a_list_like_the_tui(env):
    # The TUI passes user_message as the content list (text part + image parts), the CLI as a string.
    _blockable(env)
    env.config["block_images"] = True
    content = [{"type": "text", "text": "x" * 240000},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]
    history = [{"role": "user", "content": content}]
    out = env.plugin._on_pre_llm_call(
        conversation_history=history, user_message=content, session_id="s1", task_id="s1", model="m1",
        platform="tui", is_first_turn=False, sender_id=None, turn_id="t1", parent_session_id=None)
    assert out["action"] == "block" and out["message"].endswith(IMAGE_LINE)
    again = env.plugin._on_pre_llm_call(
        conversation_history=history, user_message=list(content), session_id="s1", task_id="s1", model="m1",
        platform="tui", is_first_turn=False, sender_id=None, turn_id="t2", parent_session_id=None)
    assert again is None  # attached again with the same text: the resend goes out


def test_hook_block_images_changes_nothing_for_plain_text(env):
    _blockable(env)
    env.config["block_images"] = True
    assert _hook(env)["message"].endswith(RETRY_LINE)


def test_hook_block_images_still_ignores_content_that_is_not_a_list_or_text(env):
    _blockable(env)
    env.config["block_images"] = True
    assert _hook(env, history=[{"role": "user", "content": None}]) is None


def test_hook_session_id_is_written_on_one_line(env):
    _blockable(env)
    seed_session(env, session_id='a"b\nc', ago=3600)
    out = _hook(env, session_id='a"b\nc')
    assert out["message"].endswith(' Session ID: a\\"b\\nc') and out["message"].count("\n") == 2


def test_hook_text_parts_are_blocked_like_a_string(env):
    _blockable(env)
    parts = [{"type": "text", "text": "x" * 240000}]
    assert _hook(env, history=[{"role": "user", "content": parts}])["action"] == "block"


def test_hook_model_change_is_found_by_the_bare_name(env):
    _blockable(env, model="p/old")
    out = _hook(env, model="new")
    assert out["message"] == (env.plugin._BLOCKED_PREFIX + "13:05 · "
                              + "model changed p/old → p/new · ~60,008 tokens may be rebilled" + RETRY_LINE)


def test_hook_same_bare_model_is_a_pause_not_a_model_change(env):
    _blockable(env, model="p/m1")
    assert "model changed" not in _hook(env, model="m1")["message"]


def test_hook_api_mode_change_is_not_a_model_change_here(env):
    _blockable(env, model="p/m1", api_mode="chat_completions")
    assert "pause" in _hook(env, model="m1")["message"]


def test_hook_model_change_small_context_is_silent(env):
    _blockable(env, model="p/old")
    assert _hook(env, "hi", model="new") is None


def test_hook_no_pause_no_block(env):
    env.config["cli"] = "block"
    seed_session(env, ago=10)
    assert _hook(env) is None


def test_hook_unknown_session_is_not_blocked(env):
    env.config["cli"] = "block"
    assert _hook(env, session_id="never-seen") is None


@pytest.mark.parametrize("history", [None, [], "x", [None], [{"role": "user"}]])
def test_hook_odd_history_lets_it_go(env, history):
    _blockable(env)
    assert env.plugin._on_pre_llm_call(conversation_history=history, user_message="x" * 240000,
                                       session_id="s1", task_id="s1", model="m1", platform="cli") is None


def test_hook_internal_error_lets_it_go(env, monkeypatch):
    _blockable(env)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(env.plugin, "_evaluate", boom)
    assert _hook(env) is None


def test_hook_lets_it_go_when_the_middleware_is_off(env, monkeypatch):
    _blockable(env)
    monkeypatch.setitem(env.plugin._H["checks"], "middleware", (False, "llm_execution: missing"))
    assert _hook(env) is None


def test_hook_garbage_arguments_let_it_go(env):
    assert env.plugin._on_pre_llm_call() is None
    assert env.plugin._on_pre_llm_call(session_id=["bad"], user_message=3) is None


def test_hook_leaves_the_middleware_to_warn(env):
    """After the hook let a message through, llm_execution warns in block mode: the provider is called."""
    _blockable(env)
    out, called, sentinel = run_middleware(env)
    assert called and out is sentinel and env.warned() is not None


def test_clean_history_drops_a_withheld_message_and_our_reply(env):
    reply = env.plugin._BLOCKED_PREFIX + "60 minutes pause 🕐 · ~60,000 tokens may be rebilled" + RETRY_LINE
    items = [
        {"role": "user", "content": "hello"}, {"role": "assistant", "content": "pong"},
        {"role": "user", "content": HELD}, {"role": "assistant", "content": reply},
        {"role": "user", "content": "question two"},
    ]
    assert env.plugin._clean_history(items) == [items[0], items[1], items[4]]
    request = {"messages": items}
    out = env.plugin._on_llm_request(request=request, session_id="s1", platform="cli")
    assert out["request"]["messages"] == [items[0], items[1], items[4]]
    assert "withheld" not in json.dumps(out["request"])


def test_clean_history_drops_the_web_ui_typed_row_too(env):
    """The web UI saves the typed text, then the placeholder, then the BLOCKED reply: both rows go."""
    reply = env.plugin._BLOCKED_PREFIX + "60 minutes pause 🕐 · ~60,000 tokens may be rebilled" + RETRY_LINE
    items = [
        {"role": "user", "content": "hello"}, {"role": "assistant", "content": "pong"},
        {"role": "user", "content": "boooo23"}, {"role": "user", "content": HELD},
        {"role": "assistant", "content": reply}, {"role": "user", "content": "question two"},
    ]
    assert env.plugin._clean_history(items) == [items[0], items[1], items[5]]


def test_clean_history_drops_only_the_withheld_row_and_the_typed_row_above(env):
    reply = env.plugin._BLOCKED_PREFIX + "60 minutes pause 🕐 · ~60,000 tokens may be rebilled" + RETRY_LINE
    items = [{"role": "user", "content": "unanswered"}, {"role": "user", "content": "typed"},
             {"role": "user", "content": HELD}, {"role": "assistant", "content": reply}]
    assert env.plugin._clean_history(items) == [items[0]]


def test_withheld_line_matches_hermes_text(env):
    assert HELD == env.plugin._WITHHELD


def test_hook_web_ui_retry_sends_the_withheld_line_and_passes(env):
    _blockable(env)
    assert _hook(env)["action"] == "block"
    history = [{"role": "user", "content": "x" * 240000}, {"role": "user", "content": "[Workspace::v1: /w]\n" + HELD}]
    assert _hook(env, HELD, history=history) is None  # the resend: not evaluated again
    assert _hook(env, HELD, history=history)["action"] == "block"  # and the pass is used up


def test_hook_withheld_line_with_nothing_held_is_checked_like_any_message(env):
    _blockable(env)
    history = [{"role": "user", "content": "x" * 240000}, {"role": "user", "content": HELD}]
    assert _hook(env, HELD, history=history)["action"] == "block"


@pytest.mark.parametrize("content, want", [
    ("second\n\n[Workspace::v1: /w]\n" + HELD, "second"),
    ("second\n\n" + HELD, "second"),
    ("a\n\nb\n\n" + HELD, "a\n\nb"),
])
def test_clean_history_cuts_the_withheld_line_out_of_a_merged_row(env, content, want):
    row = {"role": "user", "content": content, "extra": 1}
    out = env.plugin._clean_history([row])
    assert out == [{"role": "user", "content": want, "extra": 1}]
    assert row["content"] == content  # the input is not changed


def test_clean_history_cuts_the_withheld_line_out_of_text_parts(env):
    row = {"role": "user", "content": [{"type": "text", "text": "second\n\n" + HELD,
                                        "cache_control": {"type": "ephemeral"}}]}
    out = env.plugin._clean_history([row])
    assert out == [{"role": "user", "content": [{"type": "text", "text": "second",
                                                 "cache_control": {"type": "ephemeral"}}]}]
    assert row["content"][0]["text"] == "second\n\n" + HELD
    row = {"role": "user", "content": [{"type": "input_text", "text": "second\n\n" + HELD}]}
    assert env.plugin._clean_history([row])[0]["content"] == [{"type": "input_text", "text": "second"}]


def test_clean_history_drops_a_part_that_was_only_the_withheld_line(env):
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}
    row = {"role": "user", "content": [{"type": "text", "text": "look"}, image, {"type": "text", "text": HELD}]}
    assert env.plugin._clean_history([row])[0]["content"] == [{"type": "text", "text": "look"}, image]


def test_clean_history_keeps_a_row_that_is_only_the_withheld_line(env):
    rows = [{"role": "user", "content": HELD}, {"role": "user", "content": "[Workspace::v1: /w]\n" + HELD},
            {"role": "user", "content": [{"type": "text", "text": HELD}]}]
    out = env.plugin._clean_history(rows)
    assert all(a is b for a, b in zip(out, rows, strict=True))


def test_clean_history_leaves_other_rows_alone(env):
    rows = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "pong " + HELD},
            {"role": "assistant", "content": "x\n\n" + HELD},
            {"role": "tool", "tool_call_id": "c1", "content": "x\n\n" + HELD},
            {"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    out = env.plugin._clean_history(rows)
    assert all(a is b for a, b in zip(out, rows, strict=True))


def test_clean_history_drops_a_merged_row_above_a_blocked_reply(env):
    reply = env.plugin._BLOCKED_PREFIX + "60 minutes pause 🕐 · ~60,000 tokens may be rebilled" + RETRY_LINE
    items = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "pong"},
             {"role": "user", "content": "x\n\n" + HELD}, {"role": "assistant", "content": reply},
             {"role": "user", "content": "next"}]
    assert env.plugin._clean_history(items) == [items[0], items[1], items[4]]


def test_llm_request_returns_the_request_without_the_withheld_line(env):
    items = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "pong"},
             {"role": "user", "content": "second\n\n" + HELD}]
    out = env.plugin._on_llm_request(request={"messages": items}, session_id="s1", platform="cli")
    assert out["request"]["messages"] == [items[0], items[1], {"role": "user", "content": "second"}]
    assert env.plugin._on_llm_request(request={"messages": items[:2]}, session_id="s1", platform="cli") is None
