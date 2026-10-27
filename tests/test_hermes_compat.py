"""The daily Hermes check: the health script and the change-detection / alert logic (no network)."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


alert = load("hermes_compat_alert")


# --- scripts/hermes_compat.py ---

def run_compat(tmp_path, health):
    plugin = tmp_path / "plugin.py"
    plugin.write_text(f"def _health():\n    return {health!r}\n")
    return subprocess.run([sys.executable, str(SCRIPTS / "hermes_compat.py"), str(plugin)],
                          text=True, capture_output=True)


def test_compat_failed_check_exits_nonzero_and_names_it(tmp_path):
    result = run_compat(tmp_path, {"good": (True, ""), "post_message": (False, "missing or changed")})
    assert result.returncode == 1
    assert "FAIL post_message: missing or changed" in result.stdout
    assert "good" not in result.stdout


def test_compat_all_checks_pass_exits_zero(tmp_path):
    assert run_compat(tmp_path, {"good": (True, "")}).returncode == 0


def test_compat_broken_plugin_file_exits_nonzero(tmp_path):
    (tmp_path / "plugin.py").write_text("raise RuntimeError('boom')\n")
    result = subprocess.run([sys.executable, str(SCRIPTS / "hermes_compat.py"), str(tmp_path / "plugin.py")],
                            text=True, capture_output=True)
    assert result.returncode == 1 and "plugin load" in result.stdout


# --- change detection ---

def cell(ref, os_name="ubuntu-latest", ok=True, why=""):
    return {"os": os_name, "ref": ref, "ok": ok, "why": why}


def state(*cells):
    return {f"{c['ref']}|{c['os']}": c for c in cells}


def test_no_change_no_line():
    now = state(cell("v1"), cell("main", ok=False, why="compat failed"))
    assert alert.changes(now, now, latest="v1") == []


def test_pass_to_fail_and_back_one_line_each():
    ok, ko = state(cell("main")), state(cell("main", ok=False, why="compat failed: FAIL x: gone"))
    down = alert.changes(ok, ko)
    assert len(down) == 1 and "main on ubuntu-latest: now KO" in down[0] and "FAIL x: gone" in down[0]
    up = alert.changes(ko, ok)
    assert up == ["main on ubuntu-latest: now OK"]


def test_new_release_is_one_line_for_all_os():
    prev = state(cell("v1"), cell("v1", "windows-latest"), cell("main"))
    cur = state(cell("v1"), cell("v1", "windows-latest"), cell("main"),
                cell("v2"), cell("v2", "windows-latest"))
    assert alert.changes(prev, cur, latest="v2") == ["Hermes v2 out: plugin OK"]


def test_new_release_that_breaks_the_plugin_names_the_cell():
    prev = state(cell("v1"), cell("main"))
    cur = state(cell("v1"), cell("main"), cell("v2"), cell("v2", "macos-latest", ok=False, why="validate failed"))
    assert alert.changes(prev, cur, latest="v2") == ["Hermes v2 out: plugin KO on macos-latest (validate failed)"]


def test_new_cell_that_is_not_a_release_is_reported():
    prev = state(cell("v1"))
    cur = state(cell("v1"), cell("v0", ok=False, why="install failed"))
    assert alert.changes(prev, cur, latest="v1") == ["New cell v0 on ubuntu-latest: KO (install failed)"]


def test_first_run_reports_every_cell_not_a_release_line():
    lines = alert.changes({}, state(cell("v1"), cell("main")), latest="v1")
    assert len(lines) == 2 and all(line.startswith("New cell") for line in lines)


def test_cell_result_quotes_bad_lines_of_failed_steps(tmp_path):
    (tmp_path / "compat.txt").write_text("noise\nFAIL post_message: missing or changed\n")
    result = alert.cell_result("ubuntu-latest", "main",
                               {"fetch": "success", "install": "success", "compat": "failure", "pytest": "skipped"},
                               tmp_path)
    assert result["ok"] is False
    assert result["why"] == "compat failed: FAIL post_message: missing or changed"
    assert alert.cell_result("a", "b", {"install": "success", "pytest": "success"}, tmp_path)["ok"] is True


# --- report: state file, alert only on change ---

@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(alert, "send_telegram", lambda text: calls.append(("telegram", text)))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return calls


def run_report(tmp_path, prev, cur, latest=""):
    cells = tmp_path / "cells"
    cells.mkdir(exist_ok=True)
    for c in cur.values():
        (cells / f"cell-{c['os']}-{c['ref']}.json").write_text(json.dumps(c))
    prev_file = tmp_path / "prev.json"
    if prev is not None:
        prev_file.write_text(json.dumps(prev))
    out = tmp_path / "state.json"
    lines = alert.report(cells, prev_file, out, latest, "https://example.test/run/1")
    return lines, json.loads(out.read_text())


def test_report_no_change_sends_nothing_and_keeps_state(tmp_path, sent):
    now = state(cell("v1"), cell("main", ok=False, why="x"))
    lines, new_state = run_report(tmp_path, now, now, latest="v1")
    assert lines == [] and sent == [] and new_state == now


def test_report_change_sends_one_message_with_run_link(tmp_path, sent):
    prev = state(cell("v1"), cell("main"))
    cur = state(cell("v1"), cell("main", ok=False, why="compat failed"))
    lines, new_state = run_report(tmp_path, prev, cur, latest="v1")
    assert [name for name, _ in sent] == ["telegram"]
    assert all("main on ubuntu-latest: now KO" in text and "https://example.test/run/1" in text for _, text in sent)
    assert new_state == cur


def test_report_without_results_keeps_the_old_state(tmp_path, sent):
    lines = alert.report(tmp_path / "none", tmp_path / "prev.json", tmp_path / "state.json", "", "")
    assert lines == [] and sent == [] and not (tmp_path / "state.json").exists()


def test_missing_secrets_skip_telegram_without_error(monkeypatch, capsys):
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    alert.alert("text")
    assert "telegram: secrets missing, skipped" in capsys.readouterr().out


def test_a_failing_telegram_send_does_not_raise(monkeypatch, capsys):
    def boom(text):
        raise OSError("down")

    monkeypatch.setattr(alert, "send_telegram", boom)
    alert.alert("text")
    assert "telegram: failed (OSError), skipped" in capsys.readouterr().out


def test_telegram_uses_the_secrets(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    requests = []

    class Reply:
        def read(self):
            return b"{}"

    monkeypatch.setattr(alert.urllib.request, "urlopen", lambda url, data, timeout: requests.append((url, data)) or Reply())
    alert.alert("hello")
    assert requests[0][0] == "https://api.telegram.org/botT/sendMessage"
    assert b"chat_id=42" in requests[0][1] and b"hello" in requests[0][1]
