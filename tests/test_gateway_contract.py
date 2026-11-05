"""Installed Hermes contract on an isolated gateway copy and local transports."""
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HERMES = Path(os.environ['HERMES_AGENT_DIR']) if os.environ.get('HERMES_AGENT_DIR') else Path.home() / '.hermes' / 'hermes-agent'


def test_installed_gateway_hold_send_deny(tmp_path):
    import pytest
    direct = bool(os.environ.get('HERMES_PYTHON') and os.environ.get('HERMES_AGENT_DIR'))  # CI: source checkout
    launcher = shutil.which('hermes')
    if (launcher is None and not direct) or not HERMES.exists():
        pytest.skip('installed Hermes required for the gateway contract')
    python = os.environ['HERMES_PYTHON'] if direct else json.loads(subprocess.check_output([launcher, '--print-runtime-command'], text=True))[0]
    home = tmp_path / 'home'
    home.mkdir()
    # The child runs with HOME set to the temp home, so pass the resolved path explicitly.
    env = dict(os.environ, HERMES_AGENT_DIR=str(HERMES), HERMES_HOME=str(home), HOME=str(home), PYTHONDONTWRITEBYTECODE='1',
               HERMES_ENABLE_PROJECT_PLUGINS='0', HERMES_GATEWAY_SESSION='1', HERMES_DISABLE_LAZY_INSTALLS='1')
    if direct:  # the venv Python already has the dependencies
        bootstrap = (f'import sys; sys.dont_write_bytecode=True; sys.path.insert(0,{str(HERMES)!r}); sys.argv=["contract"]; '
                     f'import runpy; runpy.run_path({str(Path(__file__).resolve())!r},run_name="__main__")')
    else:
        bootstrap = (f'import sys; sys.dont_write_bytecode=True; sys.path.insert(0,{str(HERMES)!r}); '
                     'import json, site; from pathlib import Path; from pm.environments import install_key, site_packages; '
                     f'root=Path({str(HERMES)!r}); '
                     'facts=json.loads((root.parent/"installs"/install_key(root)/"facts.json").read_text()); '
                     'site.addsitedir(str(site_packages(Path(facts["packages"]["venv"]["environment"])))); '
                     # Hermes' stdlib repair entrypoint explicitly skips launch prep and activation.
                     'sys.argv=["hermes","pm","repair"]; import hermes_bootstrap; sys.argv=["contract"]; '
                     f'import runpy; runpy.run_path({str(Path(__file__).resolve())!r},run_name="__main__")')
    try:
        result = subprocess.run([python, '-I', '-c', bootstrap], env=env, text=True,
                                capture_output=True, timeout=40)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(str(exc.stdout) + str(exc.stderr))
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'GATEWAY HOLD/SEND/DENY VERIFIED' in result.stdout
    assert 'INSTALLED VALIDATE/DOCTOR VERIFIED' in result.stdout
    assert 'INSTALLED THREE USAGE MODES VERIFIED' in result.stdout


def run_gateway_contract():
    import asyncio
    import socket
    import sys
    import tempfile
    import time
    from types import SimpleNamespace

    def no_network(*a, **kw):
        raise AssertionError('network forbidden in gateway contract')
    socket.create_connection = no_network
    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network

    # Only the gateway source is copied; installed libraries/agent source stay read-only.
    with tempfile.TemporaryDirectory(prefix='cacheguard-gateway-') as workspace:
        workspace = Path(workspace)
        source = workspace / 'source'
        source.mkdir()
        shutil.copytree(HERMES / 'gateway', source / 'gateway', ignore=shutil.ignore_patterns('__pycache__'))
        sys.path.insert(0, str(source))
        plugin_dir = workspace / 'plugin'
        plugin_dir.mkdir()
        for name in (*sorted(f.name for f in ROOT.glob('*.py')), 'plugin.yaml', 'README.md', 'LICENSE'):
            if (ROOT / name).exists():
                shutil.copy2(ROOT / name, plugin_dir / name)
        home = Path(os.environ['HERMES_HOME'])
        (home/'config.yaml').write_text('plugins:\n  entries:\n    cacheguard:\n      settings:\n        cli: block\n        gateway: block\n        display_on_success: true\n        pause_m: 5\n        size_k: 20\napprovals:\n  timeout: 5\n')

        from agent.turn_api_call import perform_api_call
        from gateway.config import GatewayConfig, Platform
        from gateway.platforms.event import MessageEvent
        from gateway.run import GatewayRunner
        from gateway.run_turn_runner import TurnRunner
        from gateway.session import SessionSource
        from gateway.turn_context import TurnContext
        from hermes_cli.middleware import apply_llm_request_middleware
        from hermes_cli.plugin_dev import doctor_plugin
        from hermes_cli.plugin_validate import validate_plugin_dir
        from hermes_cli.plugins import get_plugin_manager
        from tools.approval import has_blocking_approval
        probe_env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
        validation = validate_plugin_dir(plugin_dir, probe=([sys.executable], probe_env))
        assert validation.ok, validation.failures
        doctor = doctor_plugin(plugin_dir)
        assert doctor.ok, doctor.format_text()
        print('INSTALLED VALIDATE/DOCTOR VERIFIED')

        assert Path(sys.modules['gateway.run'].__file__).is_relative_to(source)
        manager = get_plugin_manager()
        manifests = manager._scan_directory(workspace, source="user")
        assert len(manifests) == 1
        manager._load_plugin(manifests[0])
        loaded = manager._plugins[manifests[0].key or manifests[0].name]
        assert not loaded.error and loaded.enabled
        plugin = loaded.module

        def put_session(plugin, sid, record):
            with plugin.storage._storage_lock():
                plugin.storage._sessions_housekeeping()
                plugin.storage._write_json_atomic(plugin.storage._session_file(sid), record)
        manager._discovered = True

        class LocalAdapter:
            typed_command_prefix = '/'
            def __init__(self):
                self.messages = []
                self.ready = asyncio.Event()
            def pause_typing_for_chat(self, chat_id):
                pass
            def resume_typing_for_chat(self, chat_id):
                pass
            async def send(self, chat_id, content, **kw):
                self.messages.append(content)
                self.ready.set()
                return SimpleNamespace(success=True, message_id='local')

        class LocalProviderAgent:
            _disable_streaming = True
            _model_request_active = None
            _pending_redirect_lock = None
            api_mode = 'chat_completions'
            base_url = 'http://fake.invalid'
            provider = 'local'
            model = 'fixture'
            platform = 'telegram'
            thinking_callback = None
            def __init__(self, sid):
                self.session_id = sid
                self.calls = 0
                self.requests = []
            def _has_pending_redirect(self):
                return False
            def _interruptible_api_call(self, request):
                self.calls += 1
                self.requests.append(request)
                return SimpleNamespace(usage={'prompt_tokens': 30000,
                                              'prompt_tokens_details': {'cached_tokens': 29000}})
            def run_conversation(self, message, history=(), pad=120000, **kwargs):
                request = {'messages': [*history, {'role': 'user', 'content': message + 'x'*pad}]}
                # The real agent runs this middleware just before the provider call.
                request = apply_llm_request_middleware(request, session_id=self.session_id, platform=self.platform,
                        model=self.model, provider=self.provider, base_url=self.base_url, api_mode=self.api_mode,
                        api_call_count=1).payload
                result = perform_api_call(self, api_kwargs=request, _original_api_kwargs=request,
                        _llm_middleware_trace=[], _moa_prepared_request=None,
                        _retry=SimpleNamespace(), thinking_spinner=None, retry_count=0,
                        api_call_count=1, api_request_id='fixture', effective_task_id='',
                        turn_id='', interrupted=False)
                return result.response

        async def check():
            runner = GatewayRunner(GatewayConfig())
            adapter = LocalAdapter()
            runner.adapters[Platform.TELEGRAM] = adapter
            for choice in ('once', 'deny', 'timeout'):
                sid = 'fixture-' + choice
                origin = SessionSource(platform=Platform.TELEGRAM, chat_id=sid, user_id='owner')
                key = runner._session_key_for_source(origin)
                put_session(plugin, sid, {'last_api_ts': time.time()-3600, 'model': 'local/fixture',
                                          'api_mode': 'chat_completions', 'prompt_tokens': 30000})
                adapter.ready.clear()
                rows_before = len(plugin.storage._load_observations())  # rows carry no session ID
                ctx = TurnContext(source=origin, session_id=sid, session_key=key,
                                  message='fixture', _status_adapter=adapter, _status_chat_id=sid,
                                  _loop_for_step=asyncio.get_running_loop(), _run_still_current=lambda: True)
                turn = TurnRunner(runner, ctx)
                agent = LocalProviderAgent(sid)
                # Use the actual gateway worker and context propagation.
                task = asyncio.create_task(runner._run_in_executor_with_context(turn._run_conversation_with_approval,
                    agent, [], [], None, None))
                async with asyncio.timeout(5):
                    while not adapter.ready.is_set():
                        await asyncio.sleep(0.01)
                assert not task.done() and agent.calls == 0 and has_blocking_approval(key)
                assert 'cacheguard' in adapter.messages[-1] and '/approve' in adapter.messages[-1]
                command = '/approve' if choice == 'once' else '/deny'
                event = MessageEvent(text=command, source=origin, message_id='decision')
                if choice == 'once':
                    await runner._handle_approve_command(event)
                elif choice == 'deny':
                    await runner._handle_deny_command(event)
                async with asyncio.timeout(8):
                    while not task.done():
                        await asyncio.sleep(0.01)
                result = task.result()
                rows = plugin.storage._load_observations()[rows_before:]
                if choice == 'once':
                    assert agent.calls == 1 and len(rows) == 1 and rows[0]['prompt_tokens'] == 30000
                    # Real Hermes hook dispatch from the gateway worker, bound to this chat: the
                    # reply stays the plain model reply and the cacheguard line is posted as its
                    # own chat message through ctx.post_message (adapter.send).
                    import gateway.run as gateway_run
                    from agent.turn_finalizer import apply_llm_output_transform
                    from gateway.plugin_messages import bind_live_source
                    from gateway.session_context import scoped_session_key
                    runner._running = True
                    runner._gateway_loop = asyncio.get_running_loop()
                    gateway_run._gateway_runner_ref = lambda: runner
                    adapter.messages.clear()
                    def finish_turn(key=key, origin=origin, sid=sid):
                        with scoped_session_key(key), bind_live_source(origin):
                            return apply_llm_output_transform(
                                SimpleNamespace(session_id=sid, model='fixture', platform='telegram'),
                                'Hello', turn_id='contract-turn')
                    final, transformed, original = await runner._run_in_executor_with_context(finish_turn)
                    assert final == 'Hello' and not transformed and original is None, (final, transformed)
                    # One-off notices from earlier checks in this process may be posted first.
                    notice = adapter.messages[-1]
                    assert notice.startswith('CGUARD ') and '[CACHEGUARD]' not in notice, notice
                    assert 'unknown' in notice or 'reused' in notice or 'lost' in notice, notice
                else:
                    assert agent.calls == 0 and rows == [] and 'message not sent' in result.choices[0].message.content
                assert not has_blocking_approval(key)
            runner._shutdown_executor(drain_timeout=1.0)
        for mode, raw in (
            ('chat_completions', {'prompt_tokens': 30000, 'prompt_tokens_details': {'cached_tokens': 29000}}),
            ('anthropic_messages', {'input_tokens': 1000, 'cache_read_input_tokens': 29000}),
            ('codex_responses', {'input_tokens': 30000, 'input_tokens_details': {'cached_tokens': 29000}}),
        ):
            response = SimpleNamespace(usage=raw)
            # A row is kept only for a pause on a known session of the same model and API mode.
            put_session(plugin, 'usage-' + mode, {'last_api_ts': time.time()-3600, 'model': 'local/fixture',
                                                  'api_mode': mode, 'prompt_tokens': 30000})
            assert plugin.guard._on_llm_execution(next_call=lambda response=response: response, request={},
                session_id='usage-' + mode, model='fixture', provider='local', api_mode=mode,
                api_request_id=mode, api_call_count=2) is response
            row = plugin.storage._load_observations()[-1]
            assert row['prompt_tokens'] == 30000 and row['cache_read_tokens'] == 29000
        print('INSTALLED THREE USAGE MODES VERIFIED')

        # Real Hermes supplies task identity through the middleware. Detached work
        # shares the physical session but must not enter the parent's guard/state.
        from hermes_cli.middleware import run_llm_execution_middleware
        detached_sid = 'detached-contract'
        put_session(plugin, detached_sid, {'last_api_ts':time.time()-3600,
                               'model':'local/fixture','api_mode':'chat_completions','prompt_tokens':30000})
        before = plugin.storage._get_session(detached_sid)
        rows_before = len(plugin.storage._load_observations())
        response = SimpleNamespace(usage={'prompt_tokens':32000,
                                   'prompt_tokens_details':{'cached_tokens':29000}})
        assert run_llm_execution_middleware({'messages':[{'role':'user','content':'x'*120000}]},
            lambda request:response, session_id=detached_sid,task_id='review-fork',
            platform='cli',model='fixture',provider='local',api_mode='chat_completions',
            api_call_count=1,api_request_id='detached-contract:review-fork:turn:api:1') is response
        after = plugin.storage._get_session(detached_sid)
        assert after.pop('cache_touch_ts') > before['last_api_ts'] and after == before
        assert len(plugin.storage._load_observations()) == rows_before  # detached calls write no row
        print('INSTALLED DETACHED TASK ISOLATION VERIFIED')

        asyncio.run(check())
        manager.unload()
        manager._load_plugin(manifests[0])
        plugin = manager._plugins[manifests[0].key or manifests[0].name].module
        saved = plugin.commands._cmd_show('setup all block 1 2')
        assert 'could not write' not in saved and 'saved to config' in saved
        from hermes_cli.config import read_raw_config
        settings = read_raw_config()['plugins']['entries']['cacheguard']['settings']
        assert settings['pause_m'] == 1 and settings['size_k'] == 2
        manager.unload()
        manager._load_plugin(manifests[0])
        plugin = manager._plugins[manifests[0].key or manifests[0].name].module
        cfg = plugin.config._cfg()
        assert cfg['cli'] == 'block' and cfg['pause_m'] == 1 and cfg['size_k'] == 2
        before = (home / 'config.yaml').read_bytes()
        for _ in range(3):
            report = plugin.commands._cmd_show()
            assert 'pause 1 minute' in report and 'size 2k' in report
        assert (home / 'config.yaml').read_bytes() == before
        manager.unload()
        print('INSTALLED SETUP PERSISTENCE VERIFIED')
        print('GATEWAY HOLD/SEND/DENY VERIFIED')


if __name__ == '__main__':
    run_gateway_contract()
