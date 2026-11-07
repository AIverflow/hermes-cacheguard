"""Real Hermes CLI, fake provider, temp home. Opt-in: python3 -m pytest -q -m e2e"""
import re

import pytest

pytestmark = pytest.mark.e2e


def test_cli_one_shot_quiet_sends_and_shows_no_cacheguard_lines(hermes_python, make_hermes_home, fake_provider):
    home = make_hermes_home(cli="block")
    first = home.cli(hermes_python, "chat", "-q", "hello", "-Q")
    out = first.stdout + first.stderr
    assert first.returncode == 0, out
    assert "pong" in out
    sid = re.search(r"session_id: (\S+)", out).group(1)

    home.age_sessions()  # the pause is at least 1 minute: make the first call look 2 minutes old
    second = home.cli(hermes_python, "chat", "-q", "again", "-Q", "--resume", sid)
    out = second.stdout + second.stderr
    assert second.returncode == 0, out
    # A -Q run is never blocked and shows no line: the message is sent, silently. Since Hermes 0.22.7
    # its task id is the session id, so the call may still be counted in the stats.
    assert "CACHEGUARD" not in out and "CGUARD" not in out
    assert any("again" in str(r.get("messages")) for r in fake_provider.requests)
