"""Real hermes-webui (HTTP API only), fake provider, temp home. Opt-in: python3 -m pytest -q -m e2e"""
import json
import time
import urllib.error

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


def _start(webui, sid, message):
    try:
        return webui.call("/api/chat/start", {"session_id": sid, "message": message}) or True
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            return None
        raise


def test_webui_cacheguard_table_is_fenced(webui):
    obs = webui.home.home / "cacheguard"
    obs.mkdir(exist_ok=True)
    now = time.time()
    rows = [{"ts": now - 100 + i, "model": "custom/fake-model", "api_mode": "chat_completions",
             "gap_s": 400, "prompt_tokens": 30000, "cache_read_tokens": 1000,
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


def test_webui_block_shows_blocked_then_resend_sends(webui, fake_provider):
    sid = webui.call("/api/session/new", {})["session"]["session_id"]

    def messages():
        return webui.call("/api/session?session_id=" + sid)["session"].get("messages") or []

    def texts(role):
        return [str(m.get("content")) for m in messages() if m["role"] == role]

    webui.call("/api/chat/start", {"session_id": sid, "message": "hello"})
    _until(lambda: texts("assistant"), "first reply")
    webui.home.age_sessions()  # the pause is at least 1 minute: make the first call look 2 minutes old
    _until(lambda: _start(webui, sid, "second"), "second turn accepted")  # 409 while the first turn ends
    _until(lambda: any("BLOCKED" in t for t in texts("assistant")), "BLOCKED reply")
    assert not any("second" in str(r["messages"][-1]["content"]) for r in fake_provider.requests)  # not sent

    # The web UI's own /retry endpoint hands back the placeholder Hermes stores for a blocked
    # message, not the typed text: so the same text is sent again by hand.
    _until(lambda: _start(webui, sid, "second"), "resend accepted")
    _until(lambda: texts("assistant")[-1:] == ["pong"], "reply after resend")
    sent = [r["messages"] for r in fake_provider.requests if "second" in str(r["messages"][-1]["content"])]
    assert len(sent) == 1 and not any("CACHEGUARD" in str(m["content"]) for m in sent[0])
