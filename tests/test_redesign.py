import copy
import json

import pytest

from conftest import append_observation, big_request, put_session, run_middleware, seed_session


def test_cli_denial_preserves_request_and_has_no_observation(env):
    env.settings['cli'] = 'block'
    seed_session(env)
    env.approval = lambda *a, **k: 'deny'
    request = big_request()
    original = copy.deepcopy(request)
    result, called, _ = run_middleware(env, request=request)
    assert not called and 'message not sent' in result.choices[0].message.content
    assert request == original
    assert env.plugin._load_observations() == []


def test_history_keeps_early_evidence_past_twenty_thousand_rows(env):
    from test_detector import exact_row
    p = env.plugin
    first = exact_row(ts=1, model='p/m')
    other = exact_row(ts=2, model='p/other')
    p._obs_path().write_text(json.dumps(first) + '\n' + (json.dumps(other) + '\n') * 20000)
    rows = p._load_observations()
    assert len(rows) == 20001 and rows[0]['model'] == 'p/m'
    append_observation(p, dict(other, ts=3))
    assert len(p._load_observations()) == 20002


def test_every_provider_return_is_recorded_before_intake_even_without_usage(env):
    from types import SimpleNamespace
    p = env.plugin
    for i, response in enumerate((SimpleNamespace(usage=None, refusal=True),
                                  {'usage': None, 'finish_reason': 'length'}, object())):
        result = p._on_llm_execution(next_call=lambda: response, request={}, session_id='s',
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
    background = p._load_observations()[-1]
    assert background['cause'] == 'auxiliary' and background['prompt_tokens'] == 20944
    assert background['gap_s'] is None and background['prev_prompt_tokens'] is None
    now[0] = 200.0
    send(env, {'prompt_tokens': 18752, 'cached': 18560}, task_id='s')
    row = p._load_observations()[-1]
    assert row['gap_s'] == 100.0 and row['prev_prompt_tokens'] == 18731
    verdict = p.evaluate_observations(p._load_observations())[-1]
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
    env.approval = lambda *a, **kw: pytest.fail('background requested human approval')
    send(env, {'prompt_tokens': 61000, 'cached': 0}, session_id='s1', task_id='security',
         model='other-model', request=big_request(), api_request_id='s1:security:turn:api:1')
    assert env.plugin._get_session('s1') == before
    assert not env.notices


def test_historical_background_rows_cannot_promote_main_zero(env):
    from test_detector import exact_row
    background = [exact_row(ts=i, api_request_id=f's:review-{i}:turn:api:1') for i in range(3)]
    target = exact_row(ts=4, cause=None, cache_read_tokens=0)
    assert not env.plugin.evaluate_observations(background + [target])[-1]['evaluable']


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
    rows = p._load_observations()
    assert len(rows) == 14 and all(r['cause']=='auxiliary' for r in rows[1:])
    after = p._get_session('s')
    after.pop('cache_touch_ts')  # the workers read most of the chat
    assert rows[1]['prompt_tokens'] is None and after == before


def test_main_task_identity_wins_over_arbitrary_or_conflicting_request_id(env, monkeypatch):
    install_normalizer(monkeypatch)
    send(env, {'prompt_tokens':100,'cached':90}, task_id='s',
         api_request_id='s:worker:turn:api:1')
    # The recorder trusts the explicit current task identity, not an ID inference.
    assert env.plugin._get_session('s')['prompt_tokens'] == 100


def test_dispatch_is_after_hold_and_gap_uses_previous_response(env, monkeypatch):
    calls = install_normalizer(monkeypatch)
    p = env.plugin
    now = [100.0]
    monkeypatch.setattr(p.time, 'time', lambda: now[0])
    send(env, {'prompt_tokens': 60000, 'cached': 59000})
    now[0] = 500.0
    env.settings.update(cli='block', pause_m=5)
    def answer(*a, **k):
        now[0] = 550.0
        return 'once'
    env.approval = answer
    response = {'usage': {'prompt_tokens': 61000, 'cached': 59000}, 'finish_reason': 'length'}
    def provider():
        assert now[0] == 550.0
        now[0] = 575.0
        return response
    assert p._on_llm_execution(next_call=provider, request=big_request(), session_id='s',
                              model='m', provider='p', api_mode='chat_completions',
                              api_request_id='same-retry-id', api_call_count=1) is response
    row = p._load_observations()[-1]
    assert row['gap_s'] == 450.0 and row['ts'] == 575.0 and row['prev_prompt_tokens'] == 60000
    assert p._get_session('s')['last_api_ts'] == 575.0
    assert calls[-1][1:] == ('p', 'chat_completions')


def test_present_empty_usage_stays_zero_and_missing_usage_stays_null(env, monkeypatch):
    calls = install_normalizer(monkeypatch)
    send(env, {})
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


def test_failed_attempt_is_not_recorded_and_retry_return_is_recorded(env, monkeypatch):
    import pytest
    install_normalizer(monkeypatch)
    p = env.plugin
    def fail():
        raise RuntimeError('provider failed')
    with pytest.raises(RuntimeError, match='provider failed'):
        p._on_llm_execution(next_call=fail, request={}, session_id='s', api_request_id='retry')
    assert p._load_observations() == []
    send(env, {'prompt_tokens': 100}, api_request_id='retry')
    assert len(p._load_observations()) == 1


def test_concurrent_returns_have_complete_rows_and_atomic_session_state(env, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    install_normalizer(monkeypatch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: send(env, {'prompt_tokens': 100 + i, 'cached': 90},
                                    api_call_count=2, api_request_id=str(i)), range(30)))
    rows = env.plugin._load_observations()
    assert len(rows) == 30 and len({r['api_request_id'] for r in rows}) == 30
    last = rows[-1]
    rec = env.plugin._get_session('s')
    assert rec['last_api_ts'] == last['ts'] and rec['prompt_tokens'] == last['prompt_tokens']
    assert not list((env.home/'cacheguard/sessions').glob('*.tmp'))


def test_unknown_domain_never_produces_a_verdict(env):
    from test_detector import exact_row
    rows = [exact_row(api_mode=None, cause=None),
            exact_row(model='unknown/m', cause=None), exact_row(model='p/unknown', cause=None)]
    assert all(not r['evaluable'] for r in env.plugin.evaluate_observations(rows))


def test_malformed_new_domain_is_skipped_with_visible_diagnostic(env):
    from test_detector import exact_row
    p = env.plugin
    p._obs_path().write_text(json.dumps(exact_row(api_mode=None, cause=None)) + '\n'
                             + json.dumps(exact_row()) + '\n')
    assert len(p._load_observations()) == 1
    assert 'invalid observation rows skipped' in p._health_text()


def test_unanswered_cli_confirmation_cancels_without_provider(env):
    env.settings['cli'] = 'block'
    seed_session(env)
    env.approval = lambda *a, **k: None
    out, called, _ = run_middleware(env)
    assert not called and 'message not sent' in out.choices[0].message.content


def test_send_cooldown_survives_failed_provider_but_deny_does_not(env, monkeypatch):
    import pytest
    p = env.plugin
    env.settings.update(cli='block', pause_m=5)
    seed_session(env, session_id='s', ago=3600)
    decisions = []
    env.approval = lambda *a, **k: (decisions.append('once'), 'once')[1]
    now = [10000.0]
    # Keep legacy fallback old enough in this synthetic clock.
    put_session(p, 's', {'last_api_ts': 1, 'model': 'p/m', 'prompt_tokens': 30000})
    monkeypatch.setattr(p.time, 'time', lambda: now[0])
    def fail():
        raise RuntimeError('provider failed')
    kw = dict(next_call=fail, request=big_request(30000), session_id='s', provider='p', model='m',
              api_mode='chat_completions', api_call_count=1)
    with pytest.raises(RuntimeError):
        p._on_llm_execution(**kw)
    now[0] += 1
    with pytest.raises(RuntimeError):
        p._on_llm_execution(**kw)
    assert decisions == ['once']
    now[0] += 300
    env.approval = lambda *a, **k: (decisions.append('deny'), 'deny')[1]
    p._on_llm_execution(**kw)
    p._on_llm_execution(**kw)
    assert decisions == ['once', 'deny', 'deny']


def test_api_mode_change_with_same_model_opens_model_change_box(env, monkeypatch):
    install_normalizer(monkeypatch)
    send(env, {'prompt_tokens': 30000, 'cached': 29000})
    env.settings['cli'] = 'block'
    env.approval = lambda *a, **k: 'deny'
    response = send(env, {'prompt_tokens': 30000}, api_mode='anthropic_messages')
    assert response.usage is None and 'message not sent' in response.content[0].text
    assert len(env.plugin._load_observations()) == 1


def test_unquoted_yaml_off_means_off(env, monkeypatch):
    import sys
    import yaml
    raw = yaml.safe_load("plugins:\n  entries:\n    cacheguard:\n      settings:\n"
                         "        cli: off\n        desktop: off\n        gateway: off\n")
    assert raw['plugins']['entries']['cacheguard']['settings']['cli'] is False
    monkeypatch.setattr(sys.modules['hermes_cli.config'], 'read_raw_config', lambda: raw)
    cfg = env.plugin._cfg()
    assert [cfg[k] for k in ('cli', 'desktop', 'gateway')] == ['off'] * 3
    assert not env.plugin._H['config_errors']


def test_box_choices_have_a_description_so_hermes_prints_no_dangling_dash(env):
    import threading

    seen = {}

    class Owner:
        _app = object()
        _approval_lock = threading.Lock()

        def _prompt_text_input_modal(self, *, title, detail, choices, timeout):
            seen['choices'] = choices
            return 'once'

        def cb(self, *a, **k):
            return 'once'

    env.approval = Owner().cb
    handled, _ = env.plugin._confirmation_screen({}, 'title\n\ndetail', 'denied', 'm')
    assert handled and seen['choices']
    assert all(desc.strip() for _value, _label, desc in seen['choices'])


def test_warn_label_only_when_cost_is_likely(env):
    p = env.plugin
    reused = {'evaluable': True, 'cold': False, 'cache_read_tokens': 59000, 'gap_s': 3600}
    cold = {'evaluable': True, 'cold': True, 'miss_delta': 59000, 'gap_s': 3600}
    for row, label in ((reused, 'OK '), (cold, 'WARN ')):
        p._WARNED['s'] = 'note'
        p._queue_outcome({'session_id': 's'}, True, row, display_on_success=True)
        assert label in p._PENDING.pop('s')


def test_zero_cached_without_baseline_says_so(env):
    row = {'evaluable': False, 'comparable': True, 'cause': None,
           'prompt_tokens': 50000, 'cache_read_tokens': 0, 'api_mode': 'chat_completions',
           'model': 'p/m', 'gap_s': 3600}
    text = env.plugin._outcome_text(row)
    assert text == 'cache unknown, too few cache hits, 60 min'


def test_first_call_of_chat_says_it_is_normal(env):
    row = {'evaluable': False, 'cause': 'start', 'gap_s': None, 'prompt_tokens': 50000,
           'cache_read_tokens': 0, 'api_mode': 'chat_completions', 'model': 'p/m'}
    assert env.plugin._outcome_text(row) == 'cache unknown, first call'


@pytest.mark.parametrize("trigger", ["idle", "model_change"])
def test_gateway_block_without_approval_path_warns_and_says_degraded(env, monkeypatch, trigger):
    env.config.update(gateway="block", cli="off", size_k=1)
    if trigger == "idle":
        seed_session(env, ago=3600)
    else:
        seed_session(env, ago=10, model="p/old")
    monkeypatch.setattr(env.plugin, "_gateway_approval", lambda: None)
    out, called, sentinel = run_middleware(env, platform="telegram", model="new")
    assert called and out is sentinel and env.warned() is not None
    assert 'cacheguard cannot show the box here: it only warns.' in env.plugin._H['noticed']
