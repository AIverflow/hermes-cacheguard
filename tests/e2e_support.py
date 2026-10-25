"""Helpers for the opt-in `e2e` tests: a fake OpenAI-compatible provider and a temp Hermes home."""
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNNER = Path(__file__).resolve().parent / "e2e_runner.py"
AGENT_DIR = Path(os.environ.get("HERMES_AGENT_DIR") or Path.home() / ".hermes" / "hermes-agent")
WEBUI_DIR = Path(os.environ["HERMES_WEBUI_DIR"]) if os.environ.get("HERMES_WEBUI_DIR") else None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def hermes_python():
    """Interpreter of the installed Hermes, or skip."""
    launcher = shutil.which("hermes")
    if launcher is None or not AGENT_DIR.exists():
        pytest.skip("installed Hermes required for e2e tests")
    # The venv's Python, not the bare store Python: Hermes now runs CLI turns in a gateway child
    # started with the same interpreter, and the bare one refuses to boot under a temp HERMES_HOME.
    try:
        store = json.loads(subprocess.check_output([launcher, "--print-runtime-command"], text=True, timeout=20))[0]
        venv = subprocess.check_output([store, "-I", "-c", "import sys; from pathlib import Path; "
                                        f"sys.path.insert(0, {str(AGENT_DIR)!r}); "
                                        "from pm.environments import committed_venv; "
                                        f"print(committed_venv(Path({str(AGENT_DIR)!r})))"],
                                       text=True, timeout=20).strip()
        python = Path(venv) / "bin" / "python3"
        assert python.is_file(), python
        return str(python)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot find the Hermes Python: {exc}")


class FakeProvider:
    """OpenAI-compatible /v1/chat/completions answering "pong" with test-set usage."""

    def __init__(self):
        self.prompt_tokens = 30000
        self.cached_tokens = 0
        self.requests = []
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
                if not self.path.endswith("/chat/completions"):
                    self.send_response(404)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                provider.requests.append(body)
                usage = {"prompt_tokens": provider.prompt_tokens, "completion_tokens": 2,
                         "total_tokens": provider.prompt_tokens + 2,
                         "prompt_tokens_details": {"cached_tokens": provider.cached_tokens}}
                base = {"id": "fake-1", "model": "fake-model"}
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    chunks = [
                        {**base, "object": "chat.completion.chunk",
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": "pong"}, "finish_reason": None}]},
                        {**base, "object": "chat.completion.chunk",
                         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                        {**base, "object": "chat.completion.chunk", "choices": [], "usage": usage},
                    ]
                    for chunk in chunks:
                        self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                out = json.dumps({**base, "object": "chat.completion", "usage": usage, "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}]}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class HermesHome:
    """A throwaway HERMES_HOME (and HOME) with the plugin symlinked and enabled."""

    def __init__(self, provider_url, settings):
        # Not pytest's tmp_path: it keeps old dirs around and a Hermes home grows large.
        self.root = Path(tempfile.mkdtemp(prefix="cacheguard-e2e-"))
        assert Path.home() / ".hermes" not in self.root.parents
        self.home = self.root / "home"
        self.user_home = self.root / "user"
        (self.home / "plugins").mkdir(parents=True)
        self.user_home.mkdir()
        (self.home / "plugins" / "cacheguard").symlink_to(ROOT, target_is_directory=True)
        self.provider_url = provider_url
        self.write_config(**settings)

    def write_config(self, **settings):
        settings = {"pause_m": 1, "size_k": 0.1, **settings}
        lines = ["model:", "  default: fake-model", "  provider: custom", f"  base_url: {self.provider_url}",
                 "  api_key: fake-key", "plugins:", "  enabled: [cacheguard]", "  entries:", "    cacheguard:",
                 "      settings:"]
        lines += [f"        {key}: {json.dumps(value)}" for key, value in settings.items()]
        (self.home / "config.yaml").write_text("\n".join(lines) + "\n")

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("HERMES_", "OPENAI_", "ANTHROPIC_", "OPENROUTER_"))}
        env.update(HERMES_HOME=str(self.home), HOME=str(self.user_home), HERMES_AGENT_DIR=str(AGENT_DIR),
                   HERMES_DISABLE_LAZY_INSTALLS="1", PYTHONDONTWRITEBYTECODE="1",
                   HERMES_ENABLE_PROJECT_PLUGINS="0", NO_COLOR="1")
        env.update(extra)
        return env

    def cli(self, python, *args, timeout=60):
        return subprocess.run([python, "-I", str(RUNNER), "cli", *args], env=self.env(), text=True,
                              capture_output=True, timeout=timeout)

    def observations(self):
        path = self.home / "cacheguard" / "observations.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def age_sessions(self, seconds=120):
        """Make every saved chat look `seconds` older, so the next call comes after a real pause."""
        for path in (self.home / "cacheguard" / "sessions").glob("*.json"):
            rec = json.loads(path.read_text())
            for key in ("last_api_ts", "cache_touch_ts"):
                if isinstance(rec.get(key), (int, float)):
                    rec[key] -= seconds
            path.write_text(json.dumps(rec))

    def stop_gateways(self):
        """Stop the gateway children Hermes started for this home (they outlive the CLI call).

        Waits for them to exit: a gateway still shutting down writes into the home after rmtree."""
        import time
        mark = f"HERMES_HOME={self.home.resolve()}".encode()
        pids = []
        for proc in Path("/proc").glob("[0-9]*"):
            try:
                env = (proc / "environ").read_bytes().split(b"\0")
            except OSError:
                continue
            if mark in env:
                try:
                    os.kill(int(proc.name), signal.SIGTERM)
                    pids.append(int(proc.name))
                except OSError:
                    pass
        deadline = time.time() + 10
        while pids and time.time() < deadline:
            pids = [pid for pid in pids if Path(f"/proc/{pid}").exists()]
            time.sleep(0.1)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def close(self):
        self.stop_gateways()
        shutil.rmtree(self.root, ignore_errors=True)


class WebUI:
    """hermes-webui on a free port with its own state dir; talked to over HTTP only."""

    def __init__(self, python, hermes_home):
        import time
        import urllib.request
        if WEBUI_DIR is None or not (WEBUI_DIR / "server.py").exists():
            pytest.skip("hermes-webui not installed or HERMES_WEBUI_DIR unset")
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = hermes_home.env(
            HERMES_WEBUI_HOST="127.0.0.1", HERMES_WEBUI_PORT=str(self.port), HERMES_WEBUI_AGENT_DIR=str(AGENT_DIR),
            HERMES_WEBUI_STATE_DIR=str(hermes_home.root / "webui-state"), HERMES_WEBUI_PASSWORD="")
        self.log = open(hermes_home.root / "webui.log", "w")
        self.proc = subprocess.Popen([python, "-I", str(RUNNER), "web", str(WEBUI_DIR / "server.py")], env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                break
            try:
                urllib.request.urlopen(self.base + "/health", timeout=1)
                return
            except OSError:
                time.sleep(0.2)
        self.close()
        pytest.fail("hermes-webui did not start: " + (hermes_home.root / "webui.log").read_text()[-1500:])

    def call(self, path, body=None):
        import urllib.request
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data, {"content-type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=30).read())

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()
