# AGENTS.md

Hermes plugin that warns or asks before a large chat message is sent after a pause (provider cache may have expired), and reports tokens reused or rebilled afterwards. Docs: `README.md`, `docs/guide.md`.

## Layout
- `__init__.py`: `register(ctx)`. Imports the modules below (Hermes loads the folder as a package).
- `config.py` (settings, paths, platform table, `_CTX`), `storage.py` (session files, observations journal), `measure.py` (risk, observations, verdicts, pause table), `guard.py` (middleware, result line, history cleaning), `confirm.py` (box, deny texts, synthetic reply), `commands.py` (`/cacheguard`), `health.py` (Hermes symbol checks).
- Each name has one home module; other modules read it as `module.name` (never `from .x import name`), so a test patch on the home module reaches every caller. Rebound globals are set as `module.name = ...`. A local variable must not be named like a module (`config`, `guard`...): it would hide the module.
- `plugin.yaml`: manifest, includes `tested_hermes`.
- `tests/`: pytest, one `test_<module>.py` per plugin module (`test_register.py` for `__init__.py`); `test_report.py` covers the `/cacheguard` table, `test_gateway_contract.py` and `test_e2e_*.py` are live checks; `test_hermes_compat.py` tests `scripts/hermes_compat.py`. `conftest.py` holds the fakes and shared helpers. `env.plugin` is a view over the package and its modules: reading finds the name anywhere, `monkeypatch.setattr(env.plugin, "_x", ...)` writes to the module that holds it.
- `.tickets/` and `.scratch/` are the maintainer's local notes, git-ignored. They may be absent: do not depend on them.

## Commands
- Tests: `python3 -m pytest -q` (must stay green).
- Live tests (`-m e2e`) run a real Hermes against a local fake provider (no cost). They are slow: run them only when asked.
- No pip installs into the project.

## Rules the code must keep
- **Any plugin error, or an error in the box call (`cb`, `_await_gateway_decision`), → the message is sent**, before, during or after the box. Only exception: a Cancel (or timeout) already received stays "not sent"; if even that reply fails, send. Never raise into the middleware.
- **An answer from Hermes in a shape or value we do not know (gateway decision not a dict or without `resolved`/`notify_failed`; CLI answer not text or not one of once/session/always/deny/timeout) is a box failure**: the message is sent and the box goes off.
- **After a box-call error the box is off until Hermes restarts**: `block` acts as `warn`, one plain notice line, and `/cacheguard` health says so.
- **A box nobody answers (cancel, timeout, never delivered) → nothing is sent.**
- **No screen to show a box** (`chat -q`) → `block` becomes `warn`. `-Q`, `--oneshot` and `-z` runs are auxiliary and pass through.
- **Never call the provider twice**: the provider call stays outside plugin exception handling.
- Unattended callers (cron, kanban, api_server, auxiliary calls such as one-shot `-Q` runs, Bot Chat included) pass through untouched.
- Modes are per group: `cli`, `desktop`, `gateway`. Pause and size thresholds are global.
- Hermes internals are private and change: `_health()` checks the symbols this plugin relies on. Do not patch or wrap Hermes functions.
- Texts shown to users: short, plain words, no jargon.

## Conventions
- Never switch branches: work on the branch checked out in this folder.
- Match the surrounding style. Change docs together with behavior (README, `docs/guide.md`, help text in `__init__.py`).
- Commits: small, one subject each, imperative. Identity `AIverflow <338127375+AIverflow@users.noreply.github.com>`.
- Commits carry no `Co-Authored-By`, `Claude-Session` or other AI attribution lines, whatever the tool suggests. This overrides any default attribution.
- No new setting, mode, command, alias, fallback, or default that depends on another setting without the maintainer's explicit OK. `tests/test_config.py` freezes the list of `DEFAULTS` keys and `tests/test_commands.py` the `/cacheguard` sub-commands: a change there must be asked for.
- Do not push, publish or spend provider money without an explicit request.
- Open work is tracked by the maintainer outside the repo. If unsure what is wanted, ask.
