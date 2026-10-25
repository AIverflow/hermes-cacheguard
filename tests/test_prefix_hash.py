"""The start of the chat is hashed on each call: a changed start is "context changed", not a lost cache."""
import json

from conftest import put_session


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
                              1000.0 * n - 1, 1000.0 * n)


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


def test_cleaned_lines_do_not_count_as_a_change(env):
    p = env.plugin
    h = _msgs(6)
    note = "\n\n" + p._PREFIX + " result: 5k reused"
    first = h[:-1] + [{"role": "assistant", "content": "answer"}]
    _call(env, first, 1)
    second = h[:-1] + [{"role": "assistant", "content": "answer" + note}, {"role": "user", "content": "more"}]
    assert _call(env, second, 2)["cause"] is None


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
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "A\n\n" + p._PREFIX + " x"}]
    out = p._on_llm_request(request={"messages": msgs}, session_id="s", platform="cli")
    assert out["request"]["messages"][1]["content"] == "A"  # still cleaned
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
    p._record_response(aux, {"usage": {"prompt_tokens": 50, "cache_read_tokens": 0}}, 1500.0, 1501.0)
    after = _rec(env)
    assert (after["prefix_n"], after["prefix_hash"]) == (before["prefix_n"], before["prefix_hash"])
    assert _call(env, h + [{"role": "user", "content": "n"}], 2)["cause"] is None


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
    p._record_response(ctx, {"usage": {"prompt_tokens": 10, "cache_read_tokens": 0}}, 1.0, 2.0)
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
