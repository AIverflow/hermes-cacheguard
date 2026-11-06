"""Live checks against the installed Hermes: its plugin loader, and a block then /retry through its gateway runner."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAD = ' x' * 300  # about 150 tokens: over the test size threshold
HERMES = Path(os.environ['HERMES_AGENT_DIR']) if os.environ.get('HERMES_AGENT_DIR') else Path.home() / '.hermes' / 'hermes-agent'


def test_installed_plugin_contract(tmp_path):
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
    assert 'PLUGIN CONTRACT VERIFIED' in result.stdout
    assert 'INSTALLED VALIDATE/DOCTOR VERIFIED' in result.stdout
    assert 'INSTALLED THREE USAGE MODES VERIFIED' in result.stdout


def test_gateway_block_then_retry_or_resend(hermes_python, make_hermes_home, fake_provider):
    """Real gateway runner, real agent, fake provider: a blocked message never reaches the provider,
    /retry or the same text again sends it once, and the sent request carries no BLOCKED text."""
    from e2e_support import RUNNER
    home = make_hermes_home(gateway="block")
    result = subprocess.run([hermes_python, "-I", str(RUNNER), "script", str(Path(__file__).resolve()), "retry"],
                            env=home.env(GATEWAY_ALLOW_ALL_USERS="true"), text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RETRY SCENARIO VERIFIED" in result.stdout, result.stdout + result.stderr
    # The model requests, in order (the title request is another conversation). A blocked message
    # makes no request, so each of the three messages shows up exactly once, as typed.
    chats = [r["messages"] for r in fake_provider.requests if "name chat sessions" not in r["messages"][0]["content"]]
    users = [[m["content"] for m in msgs if m["role"] == "user"] for msgs in chats]
    sent = [u[-1] for u in users]
    assert sent[0].startswith("first" + PAD) and sent[1:] == ["second" + PAD, "third" + PAD], [t[:20] for t in sent]  # a first turn gets a hint added
    assert [len(u) for u in users] == [1, 2, 3]
    for msgs in chats:
        assert not any("CACHEGUARD" in str(m["content"]) or "withheld" in str(m["content"]) for m in msgs[1:])


def run_retry_scenario():
    """Runs inside the installed Hermes Python: the gateway runner takes messages from one chat."""
    import asyncio
    from types import SimpleNamespace

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from e2e_support import age_sessions
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    class LocalAdapter:
        typed_command_prefix = '/'
        def pause_typing_for_chat(self, chat_id):
            pass
        def resume_typing_for_chat(self, chat_id):
            pass
        def get_pending_message(self, session_key):
            return None
        async def send(self, chat_id, content, **kw):
            return SimpleNamespace(success=True, message_id='local')

    async def scenario():
        home = Path(os.environ['HERMES_HOME'])
        runner = GatewayRunner(GatewayConfig())
        runner.adapters[Platform.TELEGRAM] = LocalAdapter()
        origin = SessionSource(platform=Platform.TELEGRAM, chat_id='chat', user_id='owner')

        async def say(text):
            return await runner._handle_message(MessageEvent(text=text, source=origin, message_id='m'))

        async def after_pause(text):  # the cache lifetime is over: a large message is blocked
            age_sessions(home)
            return await say(text)

        assert await say('first' + PAD) == 'pong'
        blocked = await after_pause('second' + PAD)
        assert blocked.startswith('[CACHEGUARD] BLOCKED · ') and '/retry' in blocked, blocked
        assert await say('/retry') == 'pong'
        assert (await after_pause('third' + PAD)).startswith('[CACHEGUARD] BLOCKED · ')
        assert await say('third' + PAD) == 'pong'
        print('RETRY SCENARIO VERIFIED')

    asyncio.run(scenario())


def run_plugin_contract():
    import socket
    import tempfile
    import time
    from types import SimpleNamespace

    def no_network(*a, **kw):
        raise AssertionError('network forbidden in gateway contract')
    socket.create_connection = no_network
    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network

    with tempfile.TemporaryDirectory(prefix='cacheguard-gateway-') as workspace:
        workspace = Path(workspace)
        plugin_dir = workspace / 'plugin'
        plugin_dir.mkdir()
        for name in (*sorted(f.name for f in ROOT.glob('*.py')), 'plugin.yaml', 'README.md', 'LICENSE'):
            if (ROOT / name).exists():
                shutil.copy2(ROOT / name, plugin_dir / name)
        home = Path(os.environ['HERMES_HOME'])
        (home/'config.yaml').write_text('plugins:\n  entries:\n    cacheguard:\n      settings:\n        cli: block\n        gateway: block\n        display_on_success: true\n        pause_m: 5\n        size_k: 20\n')

        from hermes_cli.plugin_dev import doctor_plugin
        from hermes_cli.plugin_validate import validate_plugin_dir
        from hermes_cli.plugins import get_plugin_manager
        probe_env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
        validation = validate_plugin_dir(plugin_dir, probe=([sys.executable], probe_env))
        assert validation.ok, validation.failures
        doctor = doctor_plugin(plugin_dir)
        assert doctor.ok, doctor.format_text()
        print('INSTALLED VALIDATE/DOCTOR VERIFIED')

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
        print('PLUGIN CONTRACT VERIFIED')


if __name__ == '__main__':
    {'retry': run_retry_scenario}.get(sys.argv[1] if len(sys.argv) > 1 else '', run_plugin_contract)()
