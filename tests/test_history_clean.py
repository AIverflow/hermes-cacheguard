import json


def _history(p):
    """A realistic chat: text and list contents, a tool call, a BLOCKED pair, notes on replies."""
    note = "\n\n" + p._PREFIX + " result: 5k reused"
    blocked = p._deny_text("s1", gap_s=700.0)
    return [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Plan the trip."},
        {"role": "assistant", "content": "Sure.\n\n- day 1\n- day 2" + note},
        {"role": "user", "content": [{"type": "text", "text": "Now list hotels"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Hotels:\n1. Ace\n2. Bay" + note}]},
        {"role": "user", "content": "Book the cheapest one."},
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
        {"role": "assistant", "content": p._deny_text("s1", gap_s=900.0)},
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
