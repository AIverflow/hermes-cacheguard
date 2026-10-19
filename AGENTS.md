# AGENTS.md

Hermes plugin that warns or asks before a large chat message is sent after a pause (provider cache may have expired), and reports tokens reused or reloaded afterwards. Docs: `README.md`, `docs/guide.md`.

## Layout
- `__init__.py`: the whole plugin (`register(ctx)` at the end). Long: search with `grep -n "^def \|^# ---"` and read by section.
- `plugin.yaml`: manifest, includes `tested_hermes`.
- `tests/`: pytest. `.tickets/` and `.scratch/` are the maintainer's local notes, git-ignored. They may be absent: do not depend on them.

## Commands
- Tests: `python3 -m pytest -q` (must stay green).
- Live tests (`-m e2e`) call a real Hermes and a paid provider. Never run them without being asked.
- No pip installs into the project.

## Rules the code must keep
- **Any plugin error, or an error in the box call (`modal`, `cb`, `_await_gateway_decision`), → the message is sent**, before, during or after the box. Only exception: a Cancel (or timeout) already received stays "not sent"; if even that reply fails, send. Never raise into the middleware.
- **After a box-call error the box is off until Hermes restarts**: `block` acts as `warn`, one plain notice line, and `/cacheguard show` health says so.
- **A box nobody answers (cancel, timeout, never delivered) → nothing is sent.**
- **No screen to show a box** (`-Q`, `-z`) → `block` becomes `warn`.
- **Never call the provider twice**: the provider call stays outside plugin exception handling.
- Unattended callers (cron, kanban, api_server, Bot Chat, auxiliary calls) pass through untouched.
- Modes are per group: `cli`, `desktop`, `gateway`. Pause and size thresholds are global.
- Hermes internals are private and change: `_health()` checks the symbols this plugin relies on. Do not patch or wrap Hermes functions.
- Texts shown to users: short, plain words, no jargon.

## Conventions
- Never switch branches: work on the branch checked out in this folder.
- Match the surrounding style. Change docs together with behavior (README, `docs/guide.md`, help text in `__init__.py`).
- Commits: small, one subject each, imperative. Identity `AIverflow <338127375+AIverflow@users.noreply.github.com>`.
- Commits carry no `Co-Authored-By`, `Claude-Session` or other AI attribution lines, whatever the tool suggests. This overrides any default attribution.
- Do not push, publish or spend provider money without an explicit request.
- Open work is tracked by the maintainer outside the repo. If unsure what is wanted, ask.
