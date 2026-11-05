"""Tests of storage.py: session files, observations journal."""
import json
import os
import sys
import time

import pytest
from conftest import (
    _journal_row,
    append_observation,
    exact_row,
    put_session,
)

# ------------------------------------------------------ observation hook ---


def test_load_observations_skips_garbage(env):
    env.plugin._obs_path().write_text("not json\n\n" + json.dumps(_journal_row(ts=1.0)) + "\n")
    assert len(env.plugin._load_observations()) == 1


def test_load_observations_skips_malformed_rows_and_keeps_valid_history(env):
    valid = _journal_row(ts=1.0)
    env.plugin._obs_path().write_text('[]\n{"model":"bad","gap_s":"nan"}\n' + json.dumps(valid) + "\n")
    assert env.plugin._load_observations() == [valid]


def test_load_observations_skips_unbounded_integer_without_losing_valid_row(env):
    huge = "9" * 1000
    valid = _journal_row(ts=1.0)
    env.plugin._obs_path().write_text('{"model":"bad","gap_s":' + huge + '}\n' + json.dumps(valid) + "\n")
    assert env.plugin._load_observations() == [valid]


def test_load_observations_skips_explicit_null_timestamp(env):
    valid = _journal_row(ts=1.0)
    env.plugin._obs_path().write_text(
        json.dumps(_journal_row(ts=None)) + "\n" + json.dumps(valid) + "\n")
    assert env.plugin._load_observations() == [valid]


def test_read_observations_does_not_reuse_another_files_rows(env, tmp_path, monkeypatch):
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    first.write_text(json.dumps(_journal_row(ts=1.0)) + "\n")
    second.write_text(json.dumps(_journal_row(ts=2.0)) + "\n")
    os.utime(second, ns=(first.stat().st_atime_ns, first.stat().st_mtime_ns))
    monkeypatch.setattr(env.plugin, "_obs_path", lambda: first)
    assert env.plugin._read_observations()[0]["ts"] == 1.0
    monkeypatch.setattr(env.plugin, "_obs_path", lambda: second)
    assert env.plugin._read_observations()[0]["ts"] == 2.0


def test_rows_without_the_current_fields_are_skipped(env):
    valid = _journal_row(ts=1.0)
    old = {"model": "p/m", "gap_s": 600, "cold": True}
    env.plugin._obs_path().write_text(json.dumps(old) + "\n" + json.dumps(valid) + "\n")
    assert env.plugin._load_observations() == [valid]
    assert "invalid observation rows skipped" in env.plugin._H["data_errors"]


def test_old_journal_rows_that_nothing_reads_are_ignored_without_a_warning(env):
    ids = {"session_id": "chat-1", "api_request_id": "chat-1:chat-1:api:1", "task_id": "chat-1"}
    new = _journal_row(ts=5.0)
    old_pause = dict(_journal_row(ts=1.0), **ids)  # an old row of a main-chat pause: kept, extra fields ignored
    old_short = dict(_journal_row(ts=2.0, gap_s=4.0), **ids)  # a tool-call round trip
    old_first = dict(_journal_row(ts=3.0, gap_s=None, prev_prompt_tokens=None, cause="start"), **ids)
    old_aux = dict(_journal_row(ts=4.0, cause="auxiliary"), **ids)  # old way to mark a background call
    old_other_task = dict(_journal_row(ts=6.0), **dict(ids, task_id="review"))  # a background task of the chat
    _write_journal(env, [old_pause, old_short, old_first, old_aux, old_other_task, new])
    assert env.plugin._load_observations() == [old_pause, new]
    assert not env.plugin._H["data_errors"]
    evaluated = env.plugin.evaluate_observations(env.plugin._load_observations())
    assert sum(c["calls"] for cells in env.plugin._cold_cells(evaluated).values() for c in cells) == 2


def test_trim_removes_old_identifiers_once(env, monkeypatch):
    ids = {"session_id": "secret-chat", "api_request_id": "secret-chat:api:1", "task_id": "secret-chat"}
    old_pause = dict(_journal_row(ts=1.0), **ids)
    old_other_task = dict(_journal_row(ts=2.0), **dict(ids, task_id="review"))  # a background task: no longer told apart
    new = _journal_row(ts=3.0)
    path = _write_journal(env, [old_pause, old_other_task, new])
    path.write_text(path.read_text(encoding="utf-8") + "damaged secret-chat session_id\n", encoding="utf-8")
    with env.plugin._storage_lock():
        env.plugin._trim_observations(1000)
    text = path.read_text(encoding="utf-8")
    assert "secret" not in text.split("damaged")[0] and "api_request_id" not in text.split("damaged")[0]
    assert env.plugin._load_observations() == [_journal_row(ts=1.0), new]
    monkeypatch.setattr(env.plugin, "_replace_file", lambda *a, **k: pytest.fail("rewritten twice"))
    path.write_text("".join(ln + "\n" for ln in text.splitlines() if "damaged" not in ln), encoding="utf-8")
    with env.plugin._storage_lock():
        env.plugin._trim_observations(1000)


def test_storage_lock_uses_msvcrt_without_fcntl(env, monkeypatch):
    import types
    calls = []
    fake = types.SimpleNamespace(LK_NBLCK=1, LK_UNLCK=0,
                                 locking=lambda fd, mode, n: calls.append((mode, n)))
    monkeypatch.setattr(env.plugin, "fcntl", None)
    monkeypatch.setitem(env.plugin._storage_lock.__wrapped__.__globals__, "msvcrt", fake)  # not imported on POSIX
    with env.plugin._storage_lock():
        pass
    assert calls == [(1, 1), (0, 1)]


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


def test_storage_lock_gives_up_instead_of_waiting(env, monkeypatch):
    import time

    monkeypatch.setattr(env.plugin, "_LOCK_WAIT_S", 0.1)
    with open(env.plugin._state_dir() / ".lock", "a+") as other:
        if sys.platform == "win32":
            import msvcrt
            other.seek(0)
            msvcrt.locking(other.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(other.fileno(), fcntl.LOCK_EX)
        start = time.monotonic()
        with pytest.raises(TimeoutError), env.plugin._storage_lock():
            pass
        assert time.monotonic() - start < 2
    with env.plugin._storage_lock():  # free again
        pass


def test_housekeeping_keeps_newest_session_files(env, monkeypatch):
    import os

    env.settings["chat_files"] = 3
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

    env.settings["chat_files"] = 1
    d = env.plugin._sessions_dir()
    env.plugin._sessions_housekeeping()
    for i, name in enumerate(("a.json", "b.json")):
        f = d / name
        f.write_text("{}")
        os.utime(f, (time.time() - 100 + i,) * 2)
    env.plugin._sessions_housekeeping()
    assert (d / "a.json").exists() and (d / "b.json").exists()


def _write_journal(env, rows):
    path = env.plugin._obs_path()
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _filler(n, start=1000.0):
    return [_journal_row(ts=start + i) for i in range(n)]


def _payload(**extra):
    return dict({"session_id": "s", "provider": "p", "model": "m1", "api_mode": "chat_completions",
                 "api_request_id": "req"}, **extra)


def _respond(env, ts, prompt=1000, cached=1000, **payload):
    return env.plugin._record_response(
        _payload(**payload), {"usage": {"prompt_tokens": prompt, "cache_read_tokens": cached}}, ts - 1, ts, True)


def _seed(env, sid):
    """A session whose last call is long ago: the next answer follows a pause."""
    put_session(env.plugin, sid, {"last_api_ts": 1.0, "model": "p/m1", "api_mode": "chat_completions"})


def test_record_takes_the_previous_prompt_from_the_session_file(env):
    put_session(env.plugin, "s", {"last_api_ts": 100.0, "model": "p/m1", "api_mode": "chat_completions",
                                  "prompt_tokens": 1000})
    assert env.plugin._load_observations() == []  # no row of this session in the journal
    verdict = _respond(env, 200.0, prompt=1100, cached=1000)
    assert verdict["cause"] is None and verdict["prev_prompt_tokens"] == 1000 and verdict["gap_s"] == 99.0
    assert env.plugin._load_observations()[-1]["cause"] is None


def test_a_pause_of_a_minute_is_written_and_a_shorter_one_is_not(env):
    _seed(env, "s")
    _respond(env, 100.0)  # 99 s after the last call
    _respond(env, 159.0)  # 58 s: a tool-call round trip or a quick follow-up
    assert [r["ts"] for r in env.plugin._load_observations()] == [100.0]
    _respond(env, 220.0)  # exactly 60 s after the answer at 159
    assert [r["ts"] for r in env.plugin._load_observations()] == [100.0, 220.0]
    assert env.plugin._get_session("s")["last_api_ts"] == 220.0  # the session file follows every answer


def test_a_written_row_holds_no_session_or_request_identifier(env):
    _seed(env, "secret-session")
    _respond(env, 100.0, session_id="secret-session", task_id="secret-session", api_request_id="secret-request")
    text = env.plugin._obs_path().read_text(encoding="utf-8")
    assert "secret" not in text
    row = json.loads(text)
    assert sorted(row) == sorted(["ts", "model", "api_mode", "gap_s", "prompt_tokens", "cache_read_tokens",
                                  "cache_write_tokens", "prev_prompt_tokens", "cause"])


def test_a_background_call_writes_no_row_and_leaves_the_chat_baseline(env):
    _seed(env, "s")
    _respond(env, 1000.0, prompt=500, cached=500, task_id="background")
    assert env.plugin._load_observations() == []
    rec = env.plugin._get_session("s")
    assert rec["last_api_ts"] == 1.0 and "prompt_tokens" not in rec


def test_the_journal_is_read_only_when_a_verdict_is_due(env, monkeypatch):
    _seed(env, "s")
    reads = []
    monkeypatch.setattr(env.plugin, "_read_observations", lambda: reads.append(1) or [])
    usage = {"usage": {"prompt_tokens": 10, "cache_read_tokens": 10}}
    with monkeypatch.context() as m:
        m.setattr(env.plugin, "detect_observation", lambda *a: pytest.fail("background call"))
        assert env.plugin._record_response(_payload(task_id="bg"), usage, 1.0, 2.0, False) is None
    assert env.plugin._record_response(_payload(), usage, 1.0, 100.0, False) is None
    assert reads == []
    assert env.plugin._record_response(_payload(), usage, 199.0, 200.0, True)["gap_s"] == 99.0
    assert reads == [1]


def test_short_pause_row_still_gets_its_verdict(env):
    _seed(env, "s")
    _respond(env, 100.0, prompt=1000, cached=1000)
    verdict = _respond(env, 105.0, prompt=1100, cached=1000)
    assert verdict["gap_s"] == 4.0 and verdict["evaluable"] and not verdict["cold"]
    assert len(env.plugin._load_observations()) == 1


def test_record_counts_early_positive_reads_for_a_zero_read(env):
    # A read of 0 is judged only after 2 earlier positive reads of the model, whoever made them.
    put_session(env.plugin, "s", {"last_api_ts": 100.0, "model": "p/m1", "api_mode": "chat_completions",
                                  "prompt_tokens": 1000})
    _write_journal(env, [_journal_row(ts=float(i), prompt=1000) for i in range(1, 3)])
    verdict = _respond(env, 200.0, prompt=1000, cached=0)
    assert verdict["evaluable"] is True and verdict["cold"] is True


def test_trim_keeps_the_journal_near_its_limit_across_many_records(env, monkeypatch):
    env.settings["journal_rows"] = 1000
    path = _write_journal(env, _filler(1400))  # over the limit when the process starts
    for n in range(3):
        _seed(env, f"s{n}")
    for i in range(400):
        _respond(env, 10_000.0 + 100 * i, session_id=f"s{i % 3}")
        assert len(path.read_text(encoding="utf-8").splitlines()) <= 1000 * 1.1 + env.plugin._TRIM_EVERY
    lines = path.read_text(encoding="utf-8").splitlines()
    assert 1000 <= len(lines) <= 1000 * 1.1 + env.plugin._TRIM_EVERY
    assert json.loads(lines[-1])["ts"] == 10_000.0 + 100 * 399  # the newest row is kept


def test_trim_runs_on_the_first_record_and_then_only_every_few(env, monkeypatch):
    env.settings["journal_rows"] = 1000
    _write_journal(env, _filler(1000))
    _seed(env, "s")
    calls = []
    original = env.plugin._trim_observations
    monkeypatch.setattr(env.plugin, "_trim_observations", lambda limit: calls.append(1) or original(limit))
    for i in range(env.plugin._TRIM_EVERY):
        _respond(env, 10_000.0 + 100 * i)
    assert len(calls) == 1
    _respond(env, 20_000.0)
    assert len(calls) == 2


def test_history_keeps_early_evidence_past_twenty_thousand_rows(env):
    p = env.plugin
    first = exact_row(ts=1, model='p/m')
    other = exact_row(ts=2, model='p/other')
    p._obs_path().write_text(json.dumps(first) + '\n' + (json.dumps(other) + '\n') * 20000)
    rows = p._load_observations()
    assert len(rows) == 20001 and rows[0]['model'] == 'p/m'
    append_observation(p, dict(other, ts=3))
    assert len(p._load_observations()) == 20002
