"""Tests of confirm.py: the box, deny texts, synthetic reply."""
import json
import sys
import types

import pytest
from conftest import (
    _box_broke_notices,
    _cold_history,
    _run_counting,
    run_middleware,
    seed_session,
)


def test_confirmation_without_enough_data_has_no_probability(env):
    env.config["cli"] = "block"
    seed_session(env, ago=3600)
    prompts = []
    env.approval = lambda command, desc, **kw: (prompts.append(command), "deny")[1]
    out, called, _ = run_middleware(env)
    assert not called and "message not sent" in out.choices[0].message.content
    assert "%" not in prompts[0] and "est." not in prompts[0]


def test_confirmation_shows_counts_with_enough_data(env):
    env.config["cli"] = "block"
    seed_session(env, ago=3600)
    _cold_history(env.plugin, cold=6, warm=4)
    prompts = []
    env.approval = lambda command, desc, **kw: (prompts.append(command), "deny")[1]
    run_middleware(env)
    assert "Paused 60 minutes · ~" in prompts[0] and "tokens may be rebilled · lost 6 of 10 similar pauses" in prompts[0]


def test_deny_text_has_no_box_failed_wording(env):
    text = env.plugin._deny_text("s1", gap_s=1800, outcome="cancelled")
    assert "box failed" not in text and "(cancelled)" in text


def test_deny_texts_point_to_new_session(env):
    env.settings["cli"] = "block"
    env.approval = lambda *a, **k: "deny"
    seed_session(env, model="p/other", ago=5)
    out, called, _ = run_middleware(env, model="m1")
    assert not called and "start /new and paste it. Session ID: s1" in out.choices[0].message.content


# ------------------------------------------------- deny text / legacy key ---


def test_deny_text_cancelled_and_timeout_forms(env):
    def first(**kw):
        return env.plugin._deny_text("s1", **kw).splitlines()[0]
    assert first(gap_s=1800) == "[CACHEGUARD] BLOCKED · message not sent after a pause of 30 minutes (cancelled)"
    assert first(gap_s=1800, outcome="timeout", timeout=300) == (
        "[CACHEGUARD] BLOCKED · message not sent after a pause of 30 minutes and 300s timeout")
    assert first(cause="model changed") == "[CACHEGUARD] BLOCKED · message not sent (model changed)"


def test_deny_text_when_prompt_could_not_be_delivered(env, monkeypatch):
    def first(**kw):
        return env.plugin._deny_text("s1", **kw).splitlines()[0]
    assert first(gap_s=1800, outcome="undelivered", timeout=300) == (
        "[CACHEGUARD] BLOCKED · message not sent after a pause of 30 minutes and the prompt could not be delivered")
    assert first(outcome="undelivered", timeout=300) == (
        "[CACHEGUARD] BLOCKED · message not sent · the prompt could not be delivered")
    wait = types.ModuleType("tools.approval_gateway_wait")
    wait._await_gateway_decision = lambda *a: {"resolved": False, "choice": None, "notify_failed": True}
    monkeypatch.setitem(sys.modules, "tools.approval_gateway_wait", wait)
    monkeypatch.setattr(env.plugin, "_gateway_approval", lambda: ("key", lambda *a: None))
    def deny(outcome, timeout):
        return env.plugin._deny_text("s1", gap_s=1800, outcome=outcome, timeout=timeout)
    out = env.plugin._gateway_confirmation({"session_id": "s1"}, "cmd", deny, "m", {})
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
    assert prompts[0].startswith("[CACHEGUARD] Model changed\np/old → p/new · ~")
    assert prompts[0].endswith("tokens may be rebilled") and "full price" not in prompts[0]
    assert out.choices[0].message.content.splitlines()[0] == "[CACHEGUARD] BLOCKED · message not sent (model changed)"


@pytest.mark.parametrize("seconds,text", [
    (30, "30 seconds"), (60, "1 minute"), (3600, "60 minutes"), (179 * 60, "179 minutes"),
    (5 * 3600, "5 hours"), (47 * 3600, "47 hours"), (5 * 86400, "5 days"),
])
def test_pause_text_uses_hours_and_days_for_long_pauses(env, seconds, text):
    assert env.plugin._pause_text(seconds) == text


def _box(env):
    def deny(outcome, timeout):
        return env.plugin._deny_text("s1", gap_s=1800, outcome=outcome, timeout=timeout)
    return env.plugin._gateway_confirmation({}, "title\n\ndetail", deny, "m", {})


# ----------------------------------------------- the box and gateway decisions ---


@pytest.mark.parametrize("answer,word", [("deny", "(cancelled)"), ("timeout", "timeout")])
def test_box_cancel_and_timeout_text(env, answer, word):
    env.approval = lambda *a, **k: answer
    text = _box(env).choices[0].message.content
    assert "message not sent" in text and word in text
    assert ("timeout" in text) == (answer == "timeout")


def test_box_asks_with_session_and_always_off(env):
    seen = {}

    def cb(command, description, **kw):
        seen.update(command=command, description=description, **kw)
        return "once"

    env.approval = cb
    assert _box(env) is None
    assert seen == {"command": "title\n\ndetail", "description": "cacheguard",
                    "allow_permanent": False, "allow_session": False}


def test_cancel_reply_has_the_shape_codex_responses_reads(env):
    env.approval = lambda *a, **k: "deny"
    response = env.plugin._gateway_confirmation({"api_mode": "codex_responses"}, "cmd", None, "m", {})
    text = env.plugin._NOT_SENT_TEXT
    msg = response.output[0]
    assert response.status == "completed" and response.output_text == text
    assert (msg.type, msg.role, msg.status) == ("message", "assistant", "completed")
    assert (msg.content[0].type, msg.content[0].text) == ("output_text", text)
    assert response.usage is None and response.error is None and response.incomplete_details is None


def _cb_box(env, answer, helper_owner=False):
    env.approval = lambda *a, **k: answer
    env.config["cli"] = "block"
    seed_session(env, ago=3600)
    return _run_counting(env)


@pytest.mark.parametrize("answer", ["weird", "", 7, {"x": 1}, ["once"], True])
def test_unknown_answer_value_sends_once_and_switches_the_box_off(env, answer):
    out, calls, sentinel = _cb_box(env, answer)
    assert calls == 1 and out is sentinel
    assert len(_box_broke_notices(env)) == 1 and env.plugin._H["box_failed"] == "ValueError"


@pytest.mark.parametrize("answer,word", [("deny", "(cancelled)"), ("timeout", "timeout")])
def test_cancel_and_timeout_stay_not_sent(env, answer, word):
    out, calls, _ = _cb_box(env, answer)
    assert calls == 0 and word in out.choices[0].message.content
    assert not _box_broke_notices(env)


@pytest.mark.parametrize("answer", ["once", "session", "always"])
def test_known_send_values_send_once_without_box_failure(env, answer):
    out, calls, sentinel = _cb_box(env, answer)
    assert calls == 1 and out is sentinel and not _box_broke_notices(env)


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
    def deny(outcome, timeout):
        return env.plugin._deny_text("s1", gap_s=1800, outcome=outcome, timeout=timeout)
    out = env.plugin._gateway_confirmation({"session_id": "s1"}, "cmd", deny, "m", {})
    if sent:
        assert out is None
    else:
        assert word in out.choices[0].message.content


def test_gateway_box_with_nobody_to_ask_sends(env, monkeypatch):
    monkeypatch.setattr(env.plugin, "_gateway_approval", lambda: None)
    box = {}
    assert env.plugin._gateway_confirmation({"session_id": "s1"}, "cmd", None, "m", box) is None
    assert box == {}


@pytest.mark.parametrize("value, expected", [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("0", False), ("", False), ("no", False),
])
def test_single_query_env_fallback(env, monkeypatch, value, expected):
    # No tools.approval_context in the fakes: the import fails and the env marker decides.
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", value)
    assert env.plugin._single_query() is expected


def test_approval_timeout_reads_the_config_and_caps_it_like_hermes(env, monkeypatch):
    monkeypatch.setattr(env.plugin, "_raw_config", lambda: {"approvals": {"timeout": 99999999999}})
    assert env.plugin._approval_timeout() == env.plugin._MAX_WAIT_S
    assert env.plugin._MAX_WAIT_S in (31536000, 4294966)
    text = env.plugin._denied(lambda outcome, timeout: env.plugin._deny_text(outcome="timeout", timeout=timeout), "timeout")
    assert f"{env.plugin._MAX_WAIT_S}s timeout" in text
    monkeypatch.setattr(env.plugin, "_raw_config", lambda: {"approvals": {"timeout": 42}})
    assert env.plugin._approval_timeout() == 42


def test_approval_timeout_defaults_to_300_when_unset_or_unreadable(env, monkeypatch):
    for config in ({}, {"approvals": None}, {"approvals": {"timeout": "soon"}}, {"approvals": {"timeout": None}}):
        monkeypatch.setattr(env.plugin, "_raw_config", lambda config=config: config)
        assert env.plugin._approval_timeout() == 300
    monkeypatch.setattr(env.plugin, "_raw_config", lambda: 1 / 0)
    assert env.plugin._approval_timeout() == 300


def _denial(env, *, session_id="current-session", model_change=False):
    env.settings["cli"] = "block"
    if model_change:
        seed_session(env, session_id=session_id, model="p/old", ago=5)
    else:
        seed_session(env, session_id=session_id, ago=3600)
    env.approval = lambda *a, **k: "deny"
    kwargs = {"session_id": session_id}
    if model_change:
        kwargs["model"] = "new"
    return run_middleware(env, **kwargs)[0].choices[0].message.content


def test_idle_denial_references_current_session_and_retains_guidance(env):
    text = _denial(env)
    assert "Send it again, or start /new and paste it. Session ID: current-session" in text
    assert "session_search" not in text
    assert text.count("/new") == 1
    assert "message not sent" in text


def test_model_change_denial_references_current_session_and_tool(env):
    text = _denial(env, model_change=True)
    assert "Session ID: current-session" in text and "session_search" not in text
    assert "message not sent" in text and "/new" in text


def test_denial_without_session_omits_the_session_id(env):
    env.settings["cli"] = "block"
    seed_session(env, session_id="cached-old-session", ago=3600)
    env.approval = lambda *a, **k: "deny"
    out, called, sentinel = run_middleware(env, session_id="")
    assert called and out is sentinel
    text = env.plugin._deny_text(session_id=None)
    assert "session_search" not in text
    assert "cached-old-session" not in text
    assert "<id>" not in text and "Session ID" not in text
    assert "message not sent" in text and text.endswith("Send it again, or start /new and paste it.")


@pytest.mark.parametrize("session_id", ['session"quoted', "session\\path", "session\nline"])
def test_denial_quotes_session_id_safely(env, session_id):
    text = env.plugin._deny_text(session_id)
    assert f"Session ID: {json.dumps(session_id, ensure_ascii=False)[1:-1]}" in text
    assert len(text.splitlines()) == 2  # the id stays on one line (json escaping)
