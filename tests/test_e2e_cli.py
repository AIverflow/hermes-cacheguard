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

    home.age_sessions()  # the pause is at least 1 minute: make the first call look 2 minutes old
    second = home.cli(hermes_python, "chat", "-q", "again", "-Q", "--resume", sid)
    out = second.stdout + second.stderr
    assert second.returncode == 0, out
    assert "[CACHEGUARD]" in out
    assert "WARN · " in out  # `block` has no approval prompt in -Q: falls back to warn
    assert len(fake_provider.requests) >= 2  # plus a title call
    rows = home.observations()
    assert len(rows) == 2 and all(r["session_id"] == sid for r in rows)
    assert rows[1]["gap_s"] is not None and rows[1]["prompt_tokens"] == 30000


def test_terminal_command_summary_help_and_set(hermes_python, make_hermes_home):
    home = make_hermes_home(cli="block")
    config = home.home / "config.yaml"

    summary = home.cli(hermes_python, "cacheguard")
    out = summary.stdout + summary.stderr
    assert summary.returncode == 0, out
    assert "cli block" in out and "pause 1 minute" in out
    assert "hermes cacheguard set pause_m" in out  # the summary names the terminal command

    helped = home.cli(hermes_python, "cacheguard", "help")
    out = helped.stdout + helped.stderr
    assert helped.returncode == 0, out
    assert "Commands" in out and "pause_m" in out and "In the terminal, `hermes cacheguard` takes" in out

    saved = home.cli(hermes_python, "cacheguard", "set", "pause_m", "4")
    out = saved.stdout + saved.stderr
    assert saved.returncode == 0, out
    assert "saved to config" in out
    assert "pause_m: 4" in config.read_text()
    again = home.cli(hermes_python, "cacheguard")
    assert "pause 4 minutes" in again.stdout + again.stderr

    before = config.read_text()
    bad = home.cli(hermes_python, "cacheguard", "set", "pause_m", "abc")
    out = bad.stdout + bad.stderr
    assert bad.returncode == 1, out
    assert "pause_m must be a number" in out
    assert config.read_text() == before
