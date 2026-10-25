import json
import copy


def test_default_context_floor_is_twenty_thousand(env):
    score = env.plugin.score_risk({"prompt_tokens": 20000}, {"request": None})
    assert score["factors"]["floor_crossed"]
    assert not env.plugin.score_risk({"prompt_tokens": 19999}, {"request": None})["factors"]["floor_crossed"]


def test_score_risk_reports_gap_and_floor_independently(env):
    request = {"messages": [{"role": "user", "content": "x" * 100000}]}
    risk = env.plugin.score_risk(
        {"last_api_ts": 100, "prompt_tokens": 5000},
        {"request": request, "now": 400, "settings": {"pause_m": 5}},
    )
    assert risk["tokens_at_risk"] >= 20000
    assert risk["factors"] == {"gap_s": 300.0, "window_crossed": True, "floor_crossed": True}
    small = env.plugin.score_risk(
        {"last_api_ts": 100, "prompt_tokens": 5000},
        {"request": {}, "now": 400, "settings": {"pause_m": 5}},
    )
    assert small["tokens_at_risk"] == 5000 and small["factors"]["window_crossed"] is True
    assert small["factors"]["floor_crossed"] is False


def test_score_risk_reuses_history_when_request_is_missing(env):
    risk = env.plugin.score_risk(
        {"last_api_ts": 500, "prompt_tokens": 25000},
        {"request": None, "now": 1000, "settings": {"pause_m": 5}},
    )
    assert risk == {
        "tokens_at_risk": 25000,
        "factors": {"gap_s": 500.0, "window_crossed": True, "floor_crossed": True},
    }


def test_score_risk_uses_settings_overrides(env):
    risk = env.plugin.score_risk(
        {"last_api_ts": 90, "prompt_tokens": 3000},
        {"request": {}, "now": 200, "settings": {"pause_m": 1, "size_k": 2.5}},
    )
    assert risk["factors"] == {"gap_s": 110.0, "window_crossed": True, "floor_crossed": True}


def test_score_risk_estimates_all_provider_prompt_fields(env):
    parts = {
        "messages": [{"role": "user", "content": "m" * 80000}],
        "input": [{"role": "user", "content": "ignored when messages exist"}],
        "tools": [{"name": "tool", "schema": "t" * 30000}],
        "system": "s" * 30000,
        "instructions": "i" * 30000,
    }
    risk = env.plugin.score_risk(
        {}, {"request": parts, "now": 100, "settings": {"min_context_tokens": 1}},
    )
    request_only = env.plugin.score_risk(
        {}, {"request": {"messages": parts["messages"]}, "now": 100, "settings": {}},
    )
    assert risk["tokens_at_risk"] > request_only["tokens_at_risk"]
    flattened = env.plugin.score_risk(
        {}, {**parts, "now": 100, "settings": {"min_context_tokens": 1}},
    )
    assert flattened["tokens_at_risk"] == risk["tokens_at_risk"]


def test_score_risk_rejects_invalid_numbers_and_ignores_future_session_time(env):
    for bad in (None, -1, float("nan"), float("inf"), True):
        risk = env.plugin.score_risk(
            {"last_api_ts": bad, "prompt_tokens": bad},
            {"request": None, "now": float("nan"), "settings": {}},
        )
        assert risk == {
            "tokens_at_risk": 0,
            "factors": {"gap_s": None, "window_crossed": False, "floor_crossed": False},
        }
    future = env.plugin.score_risk(
        {"last_api_ts": 200, "prompt_tokens": 50000},
        {"request": None, "now": 100, "settings": {}},
    )
    assert future["factors"]["gap_s"] is None and not future["factors"]["window_crossed"]


def test_score_risk_does_not_mutate_inputs(env):
    state = {"last_api_ts": 1, "prompt_tokens": 10}
    payload = {"request": {"messages": [{"content": "x"}]}, "now": 2, "settings": {}}
    before = copy.deepcopy((state, payload))
    env.plugin.score_risk(state, payload)
    assert (state, payload) == before


def _tokens(env, messages):
    return env.plugin.score_risk({}, {"request": {"messages": messages}})["tokens_at_risk"]


def test_images_cost_a_flat_amount_not_their_base64_length(env):
    text = {"role": "user", "content": "word " * 4000}  # about 5k tokens
    base = _tokens(env, [text])
    blob = "data:image/png;base64," + "A" * 400_000
    chat = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": blob}}]}
    resp = {"role": "user", "content": [{"type": "input_image", "image_url": blob}]}
    anth = {"role": "user", "content": [{"type": "image", "source": {"type": "base64", "data": "A" * 400_000}}]}
    for image in (chat, resp, anth):
        messages = [text, image]
        before = json.dumps(messages)
        assert 5000 <= _tokens(env, messages) <= base + 1700
        assert json.dumps(messages) == before  # caller data untouched
    assert base >= 4900  # ordinary text is still counted by length

