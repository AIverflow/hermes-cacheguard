# AGENTS.md

Hermes plugin that warns or blocks before a large chat message is sent after a pause (provider cache may have expired), and reports tokens reused or rebilled afterwards. Docs: `README.md`, `docs/guide.md`.

## Layout
- `__init__.py`: `register(ctx)`. Imports the modules below (Hermes loads the folder as a package).
- `config.py` (settings, paths, platform table, `_CTX`), `storage.py` (session files, observations journal), `measure.py` (risk, observations, verdicts, pause table), `guard.py` (middleware, result line, history cleaning), `confirm.py` (block text), `commands.py` (`/cacheguard`), `health.py` (Hermes symbol checks).
- Each name has one home module; other modules read it as `module.name` (never `from .x import name`), so a test patch on the home module reaches every caller. Rebound globals are set as `module.name = ...`. A local variable must not be named like a module (`config`, `guard`...): it would hide the module.
- `plugin.yaml`: manifest, includes `tested_hermes`.
- `tests/`: pytest, one `test_<module>.py` per plugin module (`test_register.py` for `__init__.py`); `test_report.py` covers the `/cacheguard` table, `test_gateway_contract.py` and `test_e2e_*.py` are live checks; `test_hermes_compat.py` tests `scripts/hermes_compat.py`. `conftest.py` holds the fakes and shared helpers. `env.plugin` is a view over the package and its modules: reading finds the name anywhere, `monkeypatch.setattr(env.plugin, "_x", ...)` writes to the module that holds it.
- `.tickets/` and `.scratch/` are the maintainer's local notes, git-ignored. They may be absent: do not depend on them.

## Commands
- Tests: `python3 -m pytest -q` (must stay green).
- Live tests (`-m e2e`) run a real Hermes against a local fake provider (no cost). They are slow: run them only when asked.
- No pip installs into the project.

## Rules the code must keep
- **Any plugin error → the message is sent.** Never raise into a hook or middleware.
- **Block = the `pre_llm_call` hook returns `{"action": "block", "message": ...}`**: the message is not sent and the reply starts with `[CACHEGUARD] BLOCKED · `. The user sends it with `/retry` or by sending the same text again; that one resend passes without a new check.
- **The model never sees cacheguard's lines**: `llm_request` removes each BLOCKED reply and the message it answered.
- **A message with an image is only warned** unless `block_images` is on (`/retry` refuses media).
- **No screen to read a reply** (`chat -q`) → `block` becomes `warn`. `-Q`, `--oneshot` and `-z` runs are never blocked and show no line (a `-Q` run may still be counted in the stats).
- **Never call the provider twice**: the provider call stays outside plugin exception handling.
- Unattended callers (cron, kanban, api_server, auxiliary calls) pass through untouched. One-shot runs (`-Q`, Bot Chat `-Q` included): see the rule above.
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
