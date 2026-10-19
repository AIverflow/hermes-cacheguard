import pytest


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


def exact_row(**overrides):
    data = {
        "ts": 10, "session_id": "s", "model": "p/m", "api_mode": "anthropic",
        "api_request_id": "prev", "gap_s": 0, "prompt_tokens": 100,
        "cache_read_tokens": 95, "cache_write_tokens": 0,
        "prev_prompt_tokens": 100, "cause": "start",
    }
    data.update(overrides)
    return data


def test_model_key_qualifies_missing_provider(env):
    assert env.plugin._model_key("", "m") == "unknown/m"


def test_detection_returns_exact_schema_and_marks_first_call_start(env):
    row = env.plugin.detect_observation({"session": {}, "observations": []}, attempt())
    assert list(row) == ["ts", "session_id", "model", "api_mode", "api_request_id", "gap_s",
                         "prompt_tokens", "cache_read_tokens", "cache_write_tokens",
                         "prev_prompt_tokens", "cause"]
    assert row["gap_s"] is None and row["prev_prompt_tokens"] is None and row["cause"] == "start"


def test_detection_uses_previous_domain_prompt_and_response_to_dispatch_gap(env):
    state = {"session": exact_session(), "observations": [exact_row()]}
    row = env.plugin.detect_observation(state, attempt(dispatch_ts=17, ts=21))
    assert row["gap_s"] == 7 and row["prev_prompt_tokens"] == 100 and row["cause"] is None


def test_detection_usage_missing_wins_and_nulls_all_usage(env):
    row = env.plugin.detect_observation(
        {"session": exact_session(), "observations": [exact_row()]},
        attempt(prompt_tokens=None, cache_read_tokens=None, cache_write_tokens=None),
    )
    assert row["cause"] == "usage_missing"
    assert row["prompt_tokens"] is row["cache_read_tokens"] is row["cache_write_tokens"] is None


def test_detection_marks_rewrite_and_api_mode_switch(env):
    state = {"session": exact_session(), "observations": [exact_row()]}
    shrink = env.plugin.detect_observation(state, attempt(prompt_tokens=80))
    mode = env.plugin.detect_observation(state, attempt(api_mode="openai", model="p/m"))
    assert shrink["cause"] == "rewrite"
    assert mode["cause"] == "model_switch" and mode["prev_prompt_tokens"] is None and mode["gap_s"] == 5


def test_small_trim_after_long_pause_is_judged_for_cache_loss(env):
    state = {"session": exact_session(last_api_ts=10), "observations": [exact_row(prompt_tokens=85179)]}
    row = env.plugin.detect_observation(state, attempt(prompt_tokens=85013, cache_read_tokens=25275,
                                                       dispatch_ts=3942, ts=3943))
    assert row["cause"] is None and row["gap_s"] == 3932
    verdict = env.plugin.evaluate_observations([exact_row(prompt_tokens=85179), row])[-1]
    text = env.plugin._outcome_text(verdict)
    assert "cache lost" in text and "tokens reloaded" in text


def test_two_percent_trim_with_full_cache_read_is_judged_as_reused(env):
    state = {"session": exact_session(last_api_ts=10), "observations": [exact_row(prompt_tokens=85179)]}
    row = env.plugin.detect_observation(state, attempt(prompt_tokens=83475, cache_read_tokens=83000))
    assert row["cause"] is None
    verdict = env.plugin.evaluate_observations([exact_row(prompt_tokens=85179), row])[-1]
    text = env.plugin._outcome_text(verdict)
    assert "tokens reused" in text and "context changed" not in text


def test_twenty_percent_drop_is_still_context_changed(env):
    state = {"session": exact_session(last_api_ts=10), "observations": [exact_row(prompt_tokens=85179)]}
    row = env.plugin.detect_observation(state, attempt(prompt_tokens=68143, cache_read_tokens=68000))
    assert row["cause"] == "rewrite"
    verdict = env.plugin.evaluate_observations([exact_row(prompt_tokens=85179), row])[-1]
    assert "context changed" in env.plugin._outcome_text(verdict)


def test_legacy_session_state_has_no_exact_baseline(env):
    row = env.plugin.detect_observation(
        {"session": {"last_api_ts": 10, "model": "p/m"}, "observations": [exact_row()]}, attempt()
    )
    assert row["gap_s"] is None and row["prev_prompt_tokens"] is None and row["cause"] == "start"


def test_evaluation_classifies_delta_at_thirty_percent_and_is_read_only(env):
    rows = [exact_row(), exact_row(ts=20, cause=None, prev_prompt_tokens=100, prompt_tokens=110,
                                  cache_read_tokens=69)]
    snapshot = [dict(r) for r in rows]
    result = env.plugin.evaluate_observations(rows)
    assert result[1]["evaluable"] is True and result[1]["miss_delta"] == 31 and result[1]["cold"] is True
    assert rows == snapshot


def test_zero_uses_three_profile_wide_hits_across_sessions(env):
    positives = [exact_row(ts=i, session_id=f'prior-{i}', cache_read_tokens=90) for i in range(3)]
    target = exact_row(ts=4, session_id='new-session', cause=None, cache_read_tokens=0, prev_prompt_tokens=100)
    result = env.plugin.evaluate_observations(positives + [target])[-1]
    assert result["evaluable"] is True and result["miss_delta"] == 100 and result["cold"] is True
    unpromoted = env.plugin.evaluate_observations([exact_row(cache_read_tokens=90), target])[-1]
    assert unpromoted["evaluable"] is False and unpromoted["cold"] is None


def test_old_rows_and_noncomparable_rows_are_non_evaluable(env):
    old = {"ts": 1, "session_id": "s", "model": "p/m", "prompt_tokens": 100,
           "cache_read_tokens": 0, "gap_s": 1}
    failed = exact_row(ts=2, cause="start", prev_prompt_tokens=100)
    old_result, failed_result = env.plugin.evaluate_observations([old, failed])
    assert old_result["approximate"] is True and old_result["evaluable"] is False
    assert old_result["miss_delta"] is None and old_result["cold"] is None
    assert failed_result["evaluable"] is False and failed_result["cold"] is None


def test_zero_baseline_and_missing_usage_do_not_score(env):
    zero = exact_row(cause=None, prev_prompt_tokens=0)
    missing = exact_row(cause="usage_missing", prompt_tokens=None, cache_read_tokens=None)
    results = env.plugin.evaluate_observations([zero, missing])
    assert all(not row["evaluable"] and row["miss_delta"] is None and row["cold"] is None for row in results)


def test_rewrite_baseline_advances_and_missing_prompt_walks_back(env):
    rewritten = exact_row(prompt_tokens=145, prev_prompt_tokens=196, cause="rewrite")
    missing = exact_row(ts=2, prompt_tokens=None, cache_read_tokens=None, cache_write_tokens=None,
                        cause="usage_missing", prev_prompt_tokens=145)
    rows = [exact_row(prompt_tokens=196), rewritten, missing]
    state = {"session": exact_session(last_api_ts=12), "observations": rows}
    after_rewrite = env.plugin.detect_observation(state, attempt(prompt_tokens=145, dispatch_ts=13))
    after_missing = env.plugin.detect_observation(
        state, attempt(prompt_tokens=150, dispatch_ts=13)
    )
    assert after_rewrite["cause"] is None and after_rewrite["prev_prompt_tokens"] == 145
    assert after_missing["prev_prompt_tokens"] == 145


def test_model_switch_back_uses_original_domain_baseline(env):
    history = [exact_row(prompt_tokens=196), exact_row(model="q/m", api_mode="anthropic",
                                                        prompt_tokens=80)]
    state = {"session": exact_session(model="q/m", last_api_ts=12), "observations": history}
    back = env.plugin.detect_observation(state, attempt(model="p/m", prompt_tokens=190, dispatch_ts=15))
    assert back["cause"] == "model_switch" and back["prev_prompt_tokens"] == 196 and back["gap_s"] == 3


def test_api_mode_switch_back_uses_original_mode_baseline(env):
    history = [exact_row(prompt_tokens=196), exact_row(api_mode="openai", prompt_tokens=145)]
    state = {"session": exact_session(api_mode="openai", last_api_ts=12), "observations": history}
    back = env.plugin.detect_observation(state, attempt(api_mode="anthropic", dispatch_ts=15))
    assert back["cause"] == "model_switch" and back["prev_prompt_tokens"] == 196


def test_gap_requires_finite_exact_session_timestamp_and_clamps_negative(env):
    observations = [exact_row()]
    for bad_ts in (None, float("nan"), float("inf"), -1):
        row = env.plugin.detect_observation(
            {"session": exact_session(last_api_ts=bad_ts), "observations": observations}, attempt()
        )
        assert row["gap_s"] is None and row["prev_prompt_tokens"] is None and row["cause"] == "start"
    empty_mode = env.plugin.detect_observation(
        {"session": exact_session(api_mode=""), "observations": observations}, attempt()
    )
    assert empty_mode["gap_s"] is None and empty_mode["prev_prompt_tokens"] is None
    negative = env.plugin.detect_observation(
        {"session": exact_session(last_api_ts=20), "observations": observations}, attempt(dispatch_ts=15)
    )
    assert negative["gap_s"] == 0
    nonfinite_dispatch = env.plugin.detect_observation(
        {"session": exact_session(), "observations": observations}, attempt(dispatch_ts=float("nan"))
    )
    assert nonfinite_dispatch["gap_s"] is None


@pytest.mark.parametrize('other_fields', [
    {'model': 'q/m'}, {'model': 'p/other'}, {'api_mode': 'openai'},
    {'task_id': 'background'},
])
def test_promotion_excludes_legacy_other_routes_and_background(env, other_fields):
    old = {"session_id": "s", "model": "p/m", "cache_read_tokens": 90}
    other = exact_row(session_id="else", cache_read_tokens=90, **other_fields)
    rows = [old, other, exact_row(cache_read_tokens=90), exact_row(cache_read_tokens=90),
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
