"""Real hermes-webui (HTTP API only), fake provider, temp home. Opt-in: python3 -m pytest -q -m e2e"""
import json
import time

import pytest

pytestmark = pytest.mark.e2e


def _until(fn, what, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.3)
    pytest.fail("timed out waiting for " + what)


def test_webui_cacheguard_table_is_fenced(webui):
    obs = webui.home.home / "cacheguard"
    obs.mkdir(exist_ok=True)
    now = time.time()
    rows = [{"ts": now - 100 + i, "session_id": "seed", "model": "custom/fake-model", "api_mode": "chat_completions",
             "api_request_id": f"seed-{i}", "gap_s": 400, "prompt_tokens": 30000, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "prev_prompt_tokens": 30000, "cause": None} for i in range(6)]
    (obs / "observations.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = webui.call("/api/commands/exec", {"command": "/cacheguard"})["output"]
    lines = out.splitlines()
    assert lines.count("```") == 2, out
    start, end = [i for i, line in enumerate(lines) if line == "```"]
    fenced = lines[start + 1:end]
    assert fenced[0].startswith("model") and len(fenced) > 1, out
    assert any("fake-model" in line for line in fenced[1:]), out
    assert not any("fake-model" in line for line in lines[:start] + lines[end + 1:]), out


def test_webui_block_deny_shows_blocked(webui):
    sid = webui.call("/api/session/new", {})["session"]["session_id"]

    def messages():
        return webui.call("/api/session?session_id=" + sid)["session"].get("messages") or []

    webui.call("/api/chat/start", {"session_id": sid, "message": "hello"})
    _until(lambda: any(m["role"] == "assistant" for m in messages()), "first reply")
    time.sleep(1.2)  # over the 1 s pause
    webui.call("/api/chat/start", {"session_id": sid, "message": "second"})
    pending = _until(lambda: webui.call("/api/approval/pending?session_id=" + sid).get("pending"), "approval")
    assert "[CACHEGUARD]" in pending["command"]
    webui.call("/api/approval/respond", {"session_id": sid, "choice": "deny", "approval_id": pending["approval_id"]})
    blocked = _until(lambda: [m for m in messages() if "BLOCKED" in str(m.get("content"))], "BLOCKED reply")
    assert "message not sent" in blocked[-1]["content"]
