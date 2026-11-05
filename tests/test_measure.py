"""Tests of measure.py: risk, observations, verdicts, suggested pause."""
import copy
import json

import pytest
from conftest import exact_row


def test_estimate_counts_separate_system_prompt(env):
    p = env.plugin
    base = {"messages": [{"role": "user", "content": "x" * 4000}], "tools": []}
    def est(request):
        return p.score_risk({}, {"request": request})["tokens_at_risk"]

    plain = est(base)
    with_sys = est(dict(base, system="y" * 40000))
    with_instr = est(dict(base, instructions="y" * 40000))
    assert with_sys >= plain + 9000 and with_instr >= plain + 9000


def test_cache_lost_is_one_rule_for_every_turn(env):
    assert env.plugin._cache_lost(30, 100) and not env.plugin._cache_lost(29, 100)


def test_unknown_domain_never_produces_a_verdict(env):
    rows = [exact_row(api_mode=None, cause=None),
            exact_row(model='unknown/m', cause=None), exact_row(model='p/unknown', cause=None)]
    assert all(not r['evaluable'] for r in env.plugin.evaluate_observations(rows))


def test_malformed_new_domain_is_skipped_with_visible_diagnostic(env):
    p = env.plugin
    p._obs_path().write_text(json.dumps(exact_row(api_mode=None, cause=None)) + '\n'
                             + json.dumps(exact_row()) + '\n')
    assert len(p._load_observations()) == 1
    assert 'invalid observation rows skipped' in p._health_text()


def attempt(**overrides):
    data = {
        "ts": 20, "dispatch_ts": 15, "session_id": "s", "model": "p/m",
        "api_mode": "anthropic", "api_request_id": "r",
        "prompt_tokens": 100, "cache_read_tokens": 90, "cache_write_tokens": 0,
    }
    data.update(overrides)
    return data


def exact_session(**overrides):
    data = {"last_api_ts": 10, "model": "p/m", "api_mode": "anthropic"}
    data.update(overrides)
    return data


def test_model_key_qualifies_missing_provider(env):
    assert env.plugin._model_key("", "m") == "unknown/m"


def test_detection_returns_exact_schema_and_marks_first_call_start(env):
    row = env.plugin.detect_observation({"session": {}}, attempt())
    assert list(row) == ["ts", "model", "api_mode", "gap_s",
                         "prompt_tokens", "cache_read_tokens", "cache_write_tokens",
                         "prev_prompt_tokens", "cause"]
    assert row["gap_s"] is None and row["prev_prompt_tokens"] is None and row["cause"] == "start"


def test_detection_uses_previous_domain_prompt_and_response_to_dispatch_gap(env):
    state = {"session": exact_session(prompt_tokens=100)}
    row = env.plugin.detect_observation(state, attempt(dispatch_ts=17, ts=21))
    assert row["gap_s"] == 7 and row["prev_prompt_tokens"] == 100 and row["cause"] is None


def test_detection_usage_missing_wins_and_nulls_all_usage(env):
    row = env.plugin.detect_observation(
        {"session": exact_session(prompt_tokens=100)},
        attempt(prompt_tokens=None, cache_read_tokens=None, cache_write_tokens=None),
    )
    assert row["cause"] == "usage_missing"
    assert row["prompt_tokens"] is row["cache_read_tokens"] is row["cache_write_tokens"] is None


def test_detection_marks_rewrite_and_api_mode_switch(env):
    state = {"session": exact_session(prompt_tokens=100)}
    shrink = env.plugin.detect_observation(state, attempt(prompt_tokens=80))
    mode = env.plugin.detect_observation(state, attempt(api_mode="openai", model="p/m"))
    assert shrink["cause"] == "rewrite"
    assert mode["cause"] == "model_switch" and mode["prev_prompt_tokens"] is None and mode["gap_s"] == 5


def test_small_trim_after_long_pause_is_judged_for_cache_loss(env):
    state = {"session": exact_session(last_api_ts=10, prompt_tokens=85179)}
    row = env.plugin.detect_observation(state, attempt(prompt_tokens=85013, cache_read_tokens=25275,
                                                       dispatch_ts=3942, ts=3943))
    assert row["cause"] is None and row["gap_s"] == 3932
    verdict = env.plugin.evaluate_observations([exact_row(prompt_tokens=85179), row])[-1]
    text = env.plugin._outcome_text(verdict)
    assert text.startswith("cache lost") and "rebilled" in text


def test_two_percent_trim_with_full_cache_read_is_judged_as_reused(env):
    state = {"session": exact_session(last_api_ts=10, prompt_tokens=85179)}
    row = env.plugin.detect_observation(state, attempt(prompt_tokens=83475, cache_read_tokens=83000))
    assert row["cause"] is None
    verdict = env.plugin.evaluate_observations([exact_row(prompt_tokens=85179), row])[-1]
    text = env.plugin._outcome_text(verdict)
    assert "reused" in text and "context changed" not in text


def test_twenty_percent_drop_is_still_context_changed(env):
    state = {"session": exact_session(last_api_ts=10, prompt_tokens=85179)}
    row = env.plugin.detect_observation(state, attempt(prompt_tokens=68143, cache_read_tokens=68000))
    assert row["cause"] == "rewrite"
    verdict = env.plugin.evaluate_observations([exact_row(prompt_tokens=85179), row])[-1]
    assert "context changed" in env.plugin._outcome_text(verdict)


def test_legacy_session_state_has_no_exact_baseline(env):
    row = env.plugin.detect_observation(
        {"session": {"last_api_ts": 10, "model": "p/m", "prompt_tokens": 100}}, attempt()
    )
    assert row["gap_s"] is None and row["prev_prompt_tokens"] is None and row["cause"] == "start"


def test_evaluation_classifies_delta_at_thirty_percent_and_is_read_only(env):
    rows = [exact_row(), exact_row(ts=20, cause=None, prev_prompt_tokens=100, prompt_tokens=110,
                                  cache_read_tokens=69)]
    snapshot = [dict(r) for r in rows]
    result = env.plugin.evaluate_observations(rows)
    assert result[1]["evaluable"] is True and result[1]["miss_delta"] == 31 and result[1]["cold"] is True
    assert rows == snapshot


def test_zero_uses_two_profile_wide_hits_across_sessions(env):
    positives = [exact_row(ts=i, cache_read_tokens=90) for i in range(2)]
    target = exact_row(ts=4, cause=None, cache_read_tokens=0, prev_prompt_tokens=100)
    result = env.plugin.evaluate_observations(positives + [target])[-1]
    assert result["evaluable"] is True and result["miss_delta"] == 100 and result["cold"] is True
    assert result["needs_hits"] == 0
    unpromoted = env.plugin.evaluate_observations([exact_row(cache_read_tokens=90), target])[-1]
    assert unpromoted["evaluable"] is False and unpromoted["cold"] is None and unpromoted["needs_hits"] == 1


def test_noncomparable_rows_are_non_evaluable(env):
    failed = exact_row(ts=2, cause="start", prev_prompt_tokens=100)
    (failed_result,) = env.plugin.evaluate_observations([failed])
    assert failed_result["evaluable"] is False and failed_result["cold"] is None


def test_zero_baseline_and_missing_usage_do_not_score(env):
    zero = exact_row(cause=None, prev_prompt_tokens=0)
    missing = exact_row(cause="usage_missing", prompt_tokens=None, cache_read_tokens=None)
    results = env.plugin.evaluate_observations([zero, missing])
    assert all(not row["evaluable"] and row["miss_delta"] is None and row["cold"] is None for row in results)


def test_missing_usage_keeps_the_last_known_prompt_as_baseline(env):
    # The session file keeps the last prompt that was reported: a call without usage does not replace it.
    state = {"session": exact_session(last_api_ts=12, prompt_tokens=145)}
    after = env.plugin.detect_observation(state, attempt(prompt_tokens=150, dispatch_ts=13))
    assert after["cause"] is None and after["prev_prompt_tokens"] == 145


def test_model_switch_has_no_baseline_from_the_old_model(env):
    state = {"session": exact_session(model="q/m", last_api_ts=12, prompt_tokens=80)}
    back = env.plugin.detect_observation(state, attempt(model="p/m", prompt_tokens=190, dispatch_ts=15))
    assert back["cause"] == "model_switch" and back["prev_prompt_tokens"] is None and back["gap_s"] == 3


def test_api_mode_switch_has_no_baseline_from_the_old_mode(env):
    state = {"session": exact_session(api_mode="openai", last_api_ts=12, prompt_tokens=145)}
    back = env.plugin.detect_observation(state, attempt(api_mode="anthropic", dispatch_ts=15))
    assert back["cause"] == "model_switch" and back["prev_prompt_tokens"] is None


def test_gap_requires_finite_exact_session_timestamp_and_clamps_negative(env):
    for bad_ts in (None, float("nan"), float("inf"), -1):
        row = env.plugin.detect_observation(
            {"session": exact_session(last_api_ts=bad_ts, prompt_tokens=100)}, attempt()
        )
        assert row["gap_s"] is None and row["prev_prompt_tokens"] is None and row["cause"] == "start"
    empty_mode = env.plugin.detect_observation(
        {"session": exact_session(api_mode="", prompt_tokens=100)}, attempt()
    )
    assert empty_mode["gap_s"] is None and empty_mode["prev_prompt_tokens"] is None
    negative = env.plugin.detect_observation(
        {"session": exact_session(last_api_ts=20, prompt_tokens=100)}, attempt(dispatch_ts=15)
    )
    assert negative["gap_s"] == 0
    nonfinite_dispatch = env.plugin.detect_observation(
        {"session": exact_session(prompt_tokens=100)}, attempt(dispatch_ts=float("nan"))
    )
    assert nonfinite_dispatch["gap_s"] is None


@pytest.mark.parametrize('other_fields', [
    {'model': 'q/m'}, {'model': 'p/other'}, {'api_mode': 'openai'},
])
def test_promotion_excludes_legacy_and_other_routes(env, other_fields):
    old = {"model": "p/m", "cache_read_tokens": 90}
    other = exact_row(cache_read_tokens=90, **other_fields)
    rows = [old, other, exact_row(cache_read_tokens=90),
            exact_row(cause=None, cache_read_tokens=0, prev_prompt_tokens=100)]
    candidate = env.plugin.evaluate_observations(rows)[-1]
    assert not candidate["evaluable"]


def test_zero_target_does_not_count_its_own_or_later_positive_reads(env):
    target = exact_row(ts=1, cause=None, cache_read_tokens=0, prev_prompt_tokens=100)
    later = [exact_row(ts=i, cache_read_tokens=90) for i in range(2, 5)]
    result = env.plugin.evaluate_observations([target, *later])[0]
    assert not result["evaluable"] and result["miss_delta"] is None


def test_verdict_exact_threshold_and_floored_deficit(env):
    at_threshold = exact_row(cause=None, prev_prompt_tokens=100, cache_read_tokens=70)
    below_threshold = exact_row(cause=None, prev_prompt_tokens=100, cache_read_tokens=71)
    overread = exact_row(cause=None, prev_prompt_tokens=100, cache_read_tokens=110)
    result = env.plugin.evaluate_observations([at_threshold, below_threshold, overread])
    assert result[0]["cold"] is True and result[0]["miss_delta"] == 30
    assert result[1]["cold"] is False and result[1]["miss_delta"] == 29
    assert result[2]["cold"] is False and result[2]["miss_delta"] == 0


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
