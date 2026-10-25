import json
import pytest
from conftest import append_observation, put_session


@pytest.fixture(autouse=True)
def _show_small_tables(env):
    env.plugin._MIN_TABLE_CALLS = 0  # these tests use a handful of rows


def observation(model="p/m", *, gap=90, prompt=100, read=100, expected=100,
                cause=None, api_mode="anthropic", session_id="s", ts=1):
    return {
        "ts": ts, "session_id": session_id, "model": model, "api_mode": api_mode,
        "api_request_id": str(ts), "gap_s": gap, "prompt_tokens": prompt,
        "cache_read_tokens": read, "cache_write_tokens": 0,
        "prev_prompt_tokens": expected, "cause": cause,
    }


def test_report_uses_seven_fixed_idle_buckets_and_excludes_short_pauses(env):
    rows = [observation(gap=30, ts=i) for i in range(3)]
    rows += [observation(gap=90, read=0, ts=4),
             observation(gap=400, ts=5),
             observation(gap=3600, read=50, ts=6),
             observation(gap=9000, ts=7)]
    env.plugin._load_observations = lambda: rows
    out = env.plugin._cmd_show()
    assert all(label in out for label in ('1-6m','6-21m','21-41m','41-61m','61-91m','91-121m','>2h01'))
    assert '1-5min' not in out
    assert "0-60s" not in out and "TOTAL" not in out and "rebilled" not in out
    data = next(line for line in out.splitlines() if line.startswith('p/m  '))
    import re
    assert re.findall(r'\d+/\d+', data) == ['1/1', '0/1', '1/1', '0/1']


def test_report_cells_are_counts_with_no_percent(env):
    env.plugin._load_observations = lambda: [observation(ts=i) for i in range(25)]
    out = env.plugin._cmd_show("all")
    assert "0/25" in out and "%" not in out
    env.plugin._load_observations = lambda: [observation(ts=i) for i in range(9)]
    out = env.plugin._cmd_show("all")
    assert "0/9" in out and "%" not in out


def test_bucket_boundaries_count_each_call_in_one_column(env):
    import re
    rows = [observation(gap=30, ts=-i) for i in range(3)]  # positives so zero reads count
    rows += [observation(gap=gap, read=read, ts=i) for i, (gap, read) in enumerate([
        (1259,100), (1260,0), (2459,100), (2460,0), (3659,100), (3660,0),
        (5459,100), (5460,0), (7259,100), (7260,0),
    ])]
    old_rows = [{'model':'p/m', 'gap_s':row['gap_s'], 'cold':row['cache_read_tokens']==0}
                for row in rows]
    env.plugin._load_observations = lambda: rows + old_rows
    data = [line for line in env.plugin._cmd_show('all').splitlines() if line.startswith('p/m  ')]
    assert len(data) == 1  # old rows are not shown
    assert re.findall(r'\d+/\d+', data[0]) == ['0/1', '1/2', '1/2', '1/2', '1/2', '1/1']


def test_report_keeps_rare_cold_visible(env):
    rows = [observation(ts=i) for i in range(2000)]
    rows.append(observation(ts=2001, read=0))
    env.plugin._load_observations = lambda: rows
    assert "1/2001" in env.plugin._cmd_show('all')


def test_report_aligns_long_model_and_bucket_cells(env):
    model = "provider/" + "long-model-name-" * 5
    env.plugin._load_observations = lambda: [observation(model, gap=9000)]
    lines = env.plugin._cmd_show('all').splitlines()
    header = next(line for line in lines if line.startswith('model  '))
    data = next(line for line in lines if line.startswith(model))
    assert header.index('>2h01') + 5 == data.index('0/1') + len('0/1')
    assert data.startswith(model + '  ')


def test_report_hides_old_rows_without_mutation(env):
    import copy
    rows = [
        {'model':'p/m', 'session_id':'old', 'gap_s':500, 'cold':True},
        {'model':'p/m', 'session_id':'old', 'gap_s':500, 'cold':False},
        {'model':'p/m', 'session_id':'old', 'gap_s':5, 'cold':True},
        {'model':'p/m', 'session_id':'old', 'gap_s':None, 'cold':True},
        observation(gap=500),
    ]
    before = copy.deepcopy(rows)
    env.plugin._load_observations = lambda: rows
    out = env.plugin._cmd_show('all')
    assert 'historical' not in out and '0/1' in out
    assert '1/2' not in out and '2/3' not in out
    assert 'Older observations' not in out
    assert rows == before
    assert not env.plugin.evaluate_observations(rows)[0]['evaluable']


def test_report_includes_zero_reads_but_guard_still_requires_three_positives(env):
    rows = [observation(), observation(read=0), observation(cause='start'), observation(cause='rewrite'),
            observation(cause='usage_missing', read=None),
            observation(cause='auxiliary'), observation(gap=float('nan')),
            observation(gap=-1), {'model':'p/m','gap_s':90,'cold':True,'cause':'auxiliary'}]
    env.plugin._load_observations = lambda: rows
    verdict = env.plugin.evaluate_observations(rows)[1]
    assert not verdict['evaluable'] and verdict['cold'] is None
    out = env.plugin._cmd_show('all')
    assert '1/2' not in out and '0/1' in out  # the early zero read is not counted as a loss


def test_twelve_zero_reads_never_show_a_full_loss(env):
    rows = [observation(gap=600, read=0, ts=i) for i in range(12)]
    env.plugin._load_observations = lambda: rows
    assert not any(r['evaluable'] for r in env.plugin.evaluate_observations(rows))
    cells = env.plugin._cold_cells(rows, env.plugin.evaluate_observations(rows))
    assert sum(c['calls'] for c in cells.get('p/m', [])) == 0
    assert env.plugin._cold_counts('p/m', 600) is None
    assert '100%' not in env.plugin._cmd_show('all')


def test_report_hides_models_without_usable_data(env):
    env.plugin._load_observations = lambda: [observation(cause='start', gap=None)]
    out = env.plugin._cmd_show('all')
    assert not any(line.startswith('p/m') for line in out.splitlines())
    assert 'no usable pause observations yet' in out
    assert '(1 -)' not in out and 'unknown pause' not in out


def test_report_top_ten_and_filter_use_profile_data(env):
    rows = [observation(model=f'p/{i:02d}', ts=j) for i in range(10) for j in range(5)]
    rows += [observation(model='p/11',ts=i) for i in range(3)]
    rows.append(observation(model='p/11',read=0,ts=4))
    env.plugin._load_observations = lambda: rows
    out = env.plugin._cmd_show()
    assert '50 measured pauses' in out and '... 1 more' in out and 'p/11' not in out
    filtered = env.plugin._cmd_show('p/11')
    assert '1/4' in filtered and 'p/00' not in filtered
    assert 'p/11' in env.plugin._cmd_show('all')


def test_report_uses_positive_evidence_older_than_twenty_thousand_rows(env):
    rows = [observation(ts=i) for i in range(3)]
    rows.extend(observation(model='other/model', session_id='other', ts=i+3) for i in range(20001))
    rows.append(observation(read=0,ts=30000))
    env.plugin._load_observations = lambda: rows
    out = env.plugin._cmd_show('all')
    assert '20,005 measured pauses' in out and '1/4' in out


def test_reset_clears_observations_and_sessions(env):
    p = env.plugin
    append_observation(p, observation())
    put_session(p, "s", {"last_api_ts": 1})
    assert "cleared" in p._cmd_show("reset")
    assert not p._load_observations() and p._get_session("s") == {}


def test_command_keeps_blank_lines_for_tui_pager(env):
    env.plugin._load_observations = lambda: [observation(ts=i) for i in range(3)]
    out = env.plugin._cmd_entry("")
    assert "\n \n" in out and "\n\n" not in out
    assert out.replace("\n \n", "\n\n") == env.plugin._cmd_show("")


def test_no_usable_observations_says_why(env):
    env.plugin._load_observations = lambda: [observation(gap=30)]
    out = env.plugin._cmd_show('all')
    assert 'no usable pause observations yet' in out and 'pauses under 1 min are not shown' in out


def test_header_shows_effective_desktop_value(env):
    env.settings.update(cli='block')  # desktop has its own mode: default warn
    assert env.plugin._cmd_show().splitlines()[0].startswith('cacheguard  ·  cli block  ·  desktop warn  ·  ')


def test_header_counts_calls_recorded_and_in_table(env):
    rows = [observation(ts=i, gap=400) for i in range(4)]
    rows.append(observation(ts=9, gap=30))  # under one minute: recorded, not in the table
    env.plugin._load_observations = lambda: rows
    header = env.plugin._cmd_show().splitlines()[0]
    assert header.endswith('4 measured pauses')


def test_header_without_table_says_only_calls_recorded(env):
    env.plugin._MIN_TABLE_CALLS = 5  # this module lowers it; restore the real threshold
    env.plugin._load_observations = lambda: [observation(ts=i, gap=400) for i in range(3)]
    out = env.plugin._cmd_show()
    assert out.splitlines()[0].endswith('3 measured pauses') and 'calls recorded' not in out


def test_setup_hint_echoes_current_settings(env):
    env.settings.update(pause_m=10, size_k=30)
    out = env.plugin._cmd_show()
    assert 'setup all block 10 30' in out and 'after a 10 minute pause, size over 30k tokens' in out


def test_rebilled_count_is_capped_by_what_was_sent(env):
    rows = [observation(prompt=95000, read=1, expected=100000)]
    verdict = env.plugin.evaluate_observations(rows)[0]
    assert verdict['evaluable'] and verdict['cold']
    assert verdict['miss_delta'] <= 94999
    assert "95k rebilled" in env.plugin._outcome_text(verdict)


def _journal_path(env):
    return env.plugin._obs_path()


def test_damaged_last_line_only_loses_itself(env):
    good = [observation(ts=i, session_id=f"s{i}") for i in range(3)]
    for row in good:
        append_observation(env.plugin, row)
    cut = '{"ts": 9, "model": "p/é'.encode("utf-8")[:-1]  # ends inside a multi-byte character
    with open(_journal_path(env), "ab") as fh:
        fh.write(cut)
    append_observation(env.plugin, observation(ts=10, session_id="after"))
    rows = env.plugin._load_observations()
    assert [r["session_id"] for r in rows] == ["s0", "s1", "s2", "after"]
    assert "invalid observation rows skipped" in env.plugin._H["data_errors"]


def test_trim_survives_invalid_utf8_line(env):
    path = _journal_path(env)
    with open(path, "wb") as fh:
        for i in range(1200):
            fh.write(json.dumps({"ts": float(i), "n": i}).encode() + b"\n")
            if i == 5:
                fh.write(b'{"x": "\xc3\n')
    with env.plugin._storage_lock():
        env.plugin._trim_observations(1000)
    lines = path.read_bytes().split(b"\n")[:-1]
    assert len(lines) == 1000 and json.loads(lines[-1])["n"] == 1199


# ---- suggested pause column -------------------------------------------------
def _warmup():
    return [observation(gap=0, ts=i) for i in range(3)]  # earlier reads, so a zero read is a loss


def _gap_rows(spec, model="p/m"):
    """spec: [(gap, lost)] -> rows after a short warm-up."""
    rows = _warmup()
    rows += [observation(model=model, gap=gap, read=0 if lost else 100, ts=10 + i)
             for i, (gap, lost) in enumerate(spec)]
    return rows


def test_loss_blocks_are_monotone_and_merge_a_violating_pair(env):
    blocks = env.plugin._loss_blocks([(100, True), (200, False), (300, False), (400, True)])
    assert [(b[0], b[1], b[3]) for b in blocks] == [(100, 300, 3), (400, 400, 1)]
    assert blocks[0][2] == pytest.approx(1 / 3) and blocks[1][2] == 1.0
    shares = [b[2] for b in env.plugin._loss_blocks(
        [(g, (g * 7) % 3 == 0) for g in range(60, 1000, 13)])]
    assert shares == sorted(shares)
    assert env.plugin._loss_blocks([]) == []


def test_loss_blocks_without_violation_stay_apart(env):
    blocks = env.plugin._loss_blocks([(100, False), (200, False), (300, True)])
    assert [(b[0], b[1], b[2]) for b in blocks] == [(100, 100, 0.0), (200, 200, 0.0), (300, 300, 1.0)]


def test_short_pause_text(env):
    f = env.plugin._short_pause
    assert [f(s) for s in (300, 960, 3600, 4800, 3660, 7200)] == ["5m", "16m", "1h", "1h20", "1h01", "2h"]


def test_suggested_pause_all_kept_is_the_longest_pause_with_plus(env):
    pts = [(300.0 + 60 * i, False) for i in range(25)]
    assert env.plugin._suggested_pause(pts) == "29m+"


def test_suggested_pause_has_no_plus_when_a_later_block_exceeds(env):
    pts = [(300.0 + 10 * i, False) for i in range(30)] + [(1000.0 + i, True) for i in range(10)]
    assert env.plugin._suggested_pause(pts) == "9m"


def test_suggested_pause_stops_before_the_losses(env):
    pts = [(300.0 + 10 * i, False) for i in range(30)] + [(1000.0 + i, True) for i in range(10)]
    assert env.plugin._suggested_pause(pts) == "9m"  # last kept pause 590 s


def test_suggested_pause_none_for_early_losses(env):
    pts = [(100.0 + i, True) for i in range(5)] + [(200.0 + i, False) for i in range(20)]
    assert env.plugin._suggested_pause(pts) == "none"


def test_suggested_pause_needs_twenty_calls(env):
    assert env.plugin._suggested_pause([(300.0 + i, False) for i in range(19)]) == "—"
    assert env.plugin._suggested_pause([(300.0 + i, False) for i in range(20)]) == "5m+"


def test_cacheguard_table_has_the_suggested_column(env):
    spec = [(300 + 20 * i, False) for i in range(30)] + [(3000 + i, True) for i in range(10)]
    rows = _gap_rows(spec) + _gap_rows([(400, False)] * 3, model="p/few")[3:]
    env.plugin._load_observations = lambda: rows
    out = env.plugin._cmd_show("all")
    table = [ln for ln in out.splitlines() if ln.startswith(("model", "p/m", "p/few"))]
    assert table[0].rstrip().endswith("suggested")
    by_model = {ln.split()[0]: ln.split()[-1] for ln in table[1:]}
    assert by_model == {"p/m": "14m", "p/few": "—"}
    assert len({len(ln) for ln in table}) == 1  # same right-justified width


def test_suggested_failure_leaves_the_cell_out(env):
    def boom(points):
        raise RuntimeError("x")
    env.plugin._loss_blocks = boom
    rows = _gap_rows([(300 + 20 * i, False) for i in range(25)])
    env.plugin._load_observations = lambda: rows
    out = env.plugin._cmd_show("all")
    line = next(ln for ln in out.splitlines() if ln.startswith("p/m"))
    assert line.split()[-1] == "—" and "0/22" in line
