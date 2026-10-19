"""Real Hermes CLI, fake provider, temp home. Opt-in: python3 -m pytest -q -m e2e"""
import re

import pytest

pytestmark = pytest.mark.e2e


def test_cli_one_shot_second_turn_after_pause(hermes_python, make_hermes_home, fake_provider):
    home = make_hermes_home(cli="block")
    first = home.cli(hermes_python, "chat", "-q", "hello", "-Q")
    out = first.stdout + first.stderr
    assert first.returncode == 0, out
    assert "pong" in out
    sid = re.search(r"session_id: (\S+)", out).group(1)

    # The second process starts well over the 1 s pause after the first call.
    second = home.cli(hermes_python, "chat", "-q", "again", "-Q", "--resume", sid)
    out = second.stdout + second.stderr
    assert second.returncode == 0, out
    assert "[CACHEGUARD]" in out
    assert "WARN · " in out  # `block` has no approval prompt in -Q: falls back to warn
    assert len(fake_provider.requests) >= 2  # plus a title call
    rows = home.observations()
    assert len(rows) == 2 and all(r["session_id"] == sid for r in rows)
    assert rows[1]["gap_s"] is not None and rows[1]["prompt_tokens"] == 30000
