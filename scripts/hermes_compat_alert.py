#!/usr/bin/env python3
"""Daily Hermes check, part 2: keep each cell's last state and alert only when it changes.

A cell is one Hermes ref on one OS. Two commands (used by .github/workflows/hermes-compat.yml):

    cell    write one cell's result (JSON) from the outcomes of its steps
    report  compare all cell results with the last run's state, alert on change, write the new state

Standard library only.
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BAD_LINE = re.compile(r"^(FAIL|FAILED|ERROR|✗)")  # lines worth quoting from a step's log


def cell_result(os_name: str, ref: str, outcomes: dict, logdir: Path) -> dict:
    """One cell: ok unless a step failed. A failed step is quoted with up to 3 bad lines of its log."""
    why = []
    for step, outcome in outcomes.items():
        if outcome in ("success", "skipped"):
            continue
        try:
            lines = (logdir / f"{step}.txt").read_text(errors="replace").splitlines()
        except OSError:
            lines = []
        bad = [line.strip()[:200] for line in lines if BAD_LINE.match(line)][:3]
        why.append(f"{step} failed" + (": " + " | ".join(bad) if bad else ""))
    return {"os": os_name, "ref": ref, "ok": not why, "why": "; ".join(why)}


def changes(prev: dict, cur: dict, latest: str = "") -> list:
    """One line per change between two states ({"ref|os": cell}). No previous state: all cells are new."""
    lines = []
    new_release = bool(latest and prev and not any(c["ref"] == latest for c in prev.values()))
    for key in sorted(cur):
        cell, before = cur[key], prev.get(key)
        name = f"{cell['ref']} on {cell['os']}"
        why = "" if cell["ok"] else f" ({cell['why']})"
        if new_release and cell["ref"] == latest:
            continue  # said once, in the release line
        if before is None:
            lines.append(f"New cell {name}: {'OK' if cell['ok'] else 'KO'}{why}")
        elif before["ok"] != cell["ok"]:
            lines.append(f"{name}: now {'OK' if cell['ok'] else 'KO'}{why}")
    if new_release:
        bad = [c for c in cur.values() if c["ref"] == latest and not c["ok"]]
        verdict = "OK" if not bad else "KO on " + "; ".join(f"{c['os']} ({c['why']})" for c in bad)
        lines.insert(0, f"Hermes {latest} out: plugin {verdict}")
    return lines


def secret(name: str) -> str:
    """An environment value (a secret, or a path GitHub gives us); "" when unset."""
    return os.environ.get(name, "").strip()


def send_telegram(text: str) -> None:
    token, chat = secret("TELEGRAM_BOT_TOKEN"), secret("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("telegram: secrets missing, skipped")
        return
    body = urllib.parse.urlencode({"chat_id": chat, "text": text[:3500], "disable_web_page_preview": "true"})
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body.encode(), timeout=30).read()
    print("telegram: sent")


def alert(text: str) -> None:
    """A missing secret or a send error never fails the run."""
    try:
        send_telegram(text)
    except Exception as exc:
        print(f"telegram: failed ({type(exc).__name__}), skipped")


def summary(cur: dict, lines: list) -> str:
    rows = ["| Hermes | OS | State | Why |", "|---|---|---|---|"]
    rows += [f"| {c['ref']} | {c['os']} | {'OK' if c['ok'] else 'KO'} | {c['why']} |"
             for _, c in sorted(cur.items())]
    changed = ["", "Changes:"] + [f"- {line}" for line in lines] if lines else ["", "No change."]
    return "\n".join(rows + changed) + "\n"


def report(cells_dir: Path, prev_file: Path, state_out: Path, latest: str, run_url: str) -> list:
    cur = {}
    for path in sorted(cells_dir.rglob("cell-*.json")):
        cell = json.loads(path.read_text())
        cur[f"{cell['ref']}|{cell['os']}"] = cell
    try:
        prev = json.loads(prev_file.read_text())
    except (OSError, ValueError):
        prev = {}
        print("no previous state: every cell counts as new")
    if not cur:
        print("no cell results: nothing to compare, state kept")
        return []
    lines = changes(prev, cur, latest)
    text = summary(cur, lines)
    print(text)
    if secret("GITHUB_STEP_SUMMARY"):
        with open(secret("GITHUB_STEP_SUMMARY"), "a", encoding="utf-8") as out:
            out.write(text)
    if lines:
        alert("\n".join(lines + ["", f"Run: {run_url}"]))
    state_out.write_text(json.dumps(cur, indent=1, sort_keys=True))
    return lines


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("cell")
    one.add_argument("--os", required=True)
    one.add_argument("--ref", required=True)
    one.add_argument("--logs", type=Path, required=True, help="folder with <step>.txt logs")
    one.add_argument("--out", type=Path, required=True)
    one.add_argument("outcomes", nargs="+", help="step=outcome, in order (success, failure, skipped, ...)")
    rep = sub.add_parser("report")
    rep.add_argument("--cells", type=Path, required=True, help="folder with the cell-*.json files")
    rep.add_argument("--prev", type=Path, required=True, help="last run's state.json (may be missing)")
    rep.add_argument("--state-out", type=Path, required=True)
    rep.add_argument("--latest", default="")
    rep.add_argument("--run-url", default="")
    args = parser.parse_args(argv[1:])
    if args.command == "cell":
        outcomes = dict(pair.split("=", 1) for pair in args.outcomes)
        result = cell_result(args.os, args.ref, outcomes, args.logs)
        args.out.write_text(json.dumps(result))
        print(json.dumps(result))
    else:
        report(args.cells, args.prev, args.state_out, args.latest, args.run_url)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
