import json

import pytest

from conftest import run_middleware, seed_session


def _denial(env, *, session_id="current-session", model_change=False):
    env.settings["cli"] = "block"
    if model_change:
        env.settings["model_change"] = "block"
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
    assert "session_search" not in text and "export" not in text
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
    assert text.count("\n") == 2  # the id stays on one line (json escaping)
