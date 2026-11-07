# Cacheguard guide

The basics are in the [README](../README.md). This page covers the rest.


## Get more from your cache

How to make the cache last longer, per provider, and what to do once it is gone. Measured on a few runs: it may change.

Anthropic (Claude):

- Default cache: 5 minutes (tested). The default `pause_m` of 10 is longer, so cacheguard misses these losses.
- If you often pause more than 5 minutes: a 1-hour cache. Set `prompt_caching.cache_ttl: 1h` in the Hermes config, then `/cacheguard set pause_m 55`. Tested: still kept after 30 minutes. Writing to this cache costs 2x the input price instead of 1.25x (Anthropic docs).
- Or keep the 5-minute cache and set `pause_m` below it: `/cacheguard set pause_m 4`.

OpenRouter:

- By default, OpenRouter may switch provider after 10 minutes without a message (OpenRouter docs), and the new one has no cache.
- Pin one provider per model in the Hermes config:

```yaml
provider_routing:
  models:
    deepseek/deepseek-v4.1-flash:
      only:
        - deepinfra
```

- Measured: on OpenRouter, a DeepSeek model through DeepInfra keeps its cache about 2 hours.

DeepSeek as provider:

- Measured: its cache lasts 3 days to a week, so this plugin makes less sense.

A different pause for each model or provider: see [per-model settings](#per-model-settings).

Once a large chat has gone cold, it often costs less to start over: type `/new`, then ask the new chat to read back the old session by its ID (for example the first and last messages). Hermes can read a session by its ID with its `session_search` tool.

Sources: [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching), [OpenRouter prompt caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching), [DeepSeek context caching](https://api-docs.deepseek.com/guides/kv_cache/).


## Groups

Each chat belongs to one group, by the platform Hermes reports.

| Group | Covers |
|---|---|
| `cli` | terminal, editor |
| `desktop` | desktop app, web UI |
| `gateway` | Telegram, Discord, Slack and every other messaging platform, including platforms the plugin does not know |

Unattended calls (cron, kanban, subagents, the curator, API server, webhooks) are excluded. This is not a group you can set.

Each group has its own mode (`off`, `warn`, `block`). Pause and size are shared.

`setup all …` sets the three groups, even one that is off. To change one group's mode and nothing else: `/cacheguard set gateway block`.

## Settings

Set them with `/cacheguard set <name> <value>`, or in `<hermes home>/config.yaml` under `plugins.entries.cacheguard.settings` (see [config-example.yaml](config-example.yaml)). The names are the same in both. Quote `off` and `on` in YAML (`gateway: "off"`), or YAML reads them as `false` and `true`. Modes are lower case: `off`, `warn`, `block`.

| Name | Values | Default | Meaning |
|---|---|---|---|
| `cli` | off, warn, block | warn | Terminal, editor |
| `desktop` | off, warn, block | warn | Desktop app, web UI |
| `gateway` | off, warn, block | off | Gateway (messaging platforms) |
| `display_on_success` | on, off (in config.yaml: true, false) | off | Show the result line when the cache was kept |
| `block_images` | on, off (in config.yaml: true, false) | off | In `block` mode, also block a message with an image |
| `tiny_block` | on, off (in config.yaml: true, false) | off | In `block` mode, show the BLOCKED text on one short line |
| `pause_m` | number, 1 or more | 10 | Pause length that counts as a pause, in minutes |
| `size_k` | number, 0 to 2000, in thousands of tokens (`20` = 20k) | 20 | Smallest prompt to act on |
| `journal_rows` | whole number, 1 or more | 10000 | Most calls kept in the stats (a count, not days) |
| `chat_files` | whole number, 1 or more | 200 | Most chats that keep a tracking file, newest first |
| `models` | list of lines | none | Own pause, size or mode for single models, see below |

### Per-model settings

A model can have its own pause, size or mode. A line names the model as `provider/model`, exactly as in the `/cacheguard` table, and sets any of `pause_m`, `size_k`, `mode`. What a line does not set stays global.

```yaml
plugins:
  entries:
    cacheguard:
      settings:
        models:
          - {model: deepseek/deepseek-v4-flash, mode: "off"}
          - {model: openrouter/deepseek/deepseek-v4-flash, pause_m: 10}
          - {model: anthropic/claude-opus-5-5, pause_m: 5, size_k: 30}
```

In a chat, `/cacheguard set model pause_m 5` writes the line for the model of that chat, and `/cacheguard set model reset` removes it. `/cacheguard` lists the lines under the settings. To set mode, pause and size of the line in one go: `/cacheguard setup model block 10 50`.

- The name must match exactly, with no `*`. The same model at two providers is two lines.
- A line beats the global value. Pause and size are used as given.
- A line's `mode` replaces the mode of every group that is not off: `mode: block` blocks that model where its group warns; `mode: warn` never blocks it; `mode: "off"` shows nothing, but its calls are still recorded in the stats. A group set to off stays off.
- A line that is not valid is ignored, and `/cacheguard` says so on a `health: degraded` line.
- Known limit: when a message is checked for a block, Hermes gives only the model name, so the provider is the one the chat used last. After you switch provider and keep the same model name, the first message uses the old provider's line.

Other commands:

| Command | What it does |
|---|---|
| `/cacheguard all` | Table with every recorded model |
| `/cacheguard <model>` | Only models whose name contains the text |
| `/cacheguard reset` | Delete measurements and session tracking. Settings are kept |

## Model change

A cache belongs to one model. If you switch model or provider in a large chat, the block text says `model changed a → b` (in `warn`, the result line ends `, model changed`): the new model has no cache yet, so the whole chat is billed again. No pause is needed. Like the pause block, it shows only when the group is `warn` or `block` and the chat is over the size limit.

If you switch back quickly (A → B → A), A's cache may still be there. The result line shows what was really reused.


## The estimate and the table

cacheguard records each call that follows a pause of 1 minute or more (tokens sent, tokens read from the cache) in `<hermes home>/cacheguard/`, one history per profile. The history holds no session ID. Nothing leaves your machine.

It also keeps a small file for each of the last `chat_files` chats (200 by default). A chat it has no file for is not blocked until its first reply: a chat whose file was dropped, a chat started before cacheguard was on, or after `/cacheguard reset`.

A call lost the cache when at least 30% of the previous prompt was not read from it. The block text says `lost 4 of 10 similar pauses`: how often that happened before, for the same model after a similar pause. It shows after 10 such calls.

`/cacheguard` shows the same data as a table (some columns left out; the pause columns go up to over 2h01, `suggested` is last):

    model                   1-6m  6-21m  21-41m  suggested
    openai-codex/gpt-6-sol  0/14   2/17     2/4        16m

- `2/17`: 2 of 17 calls lost the cache. Every cell is `lost/calls`, no percent. The block text shows a count only from 10 calls in that pause length.
- `suggested`: the longest pause where the cache was lost in 10% of calls or fewer, on a curve that never goes down as the pause grows (isotonic fit). `none` when even the shortest pauses lose more. Shown from 20 measurements of that model, otherwise `—`. At least 5 measurements must be at that pause or longer.
- `+`: no loss seen beyond that pause; the cache may last longer.
- Not counted: pauses under 1 minute, the first call of a chat, model switches, calls where the chat itself changed (`context changed`), background calls.
- `context changed`: the start of the chat was not what the last call sent, for example Hermes edited an old message. cacheguard compares a fingerprint of the messages, kept in the chat's file (the messages themselves are not stored, and nothing is sent to the provider). Cache markers are ignored; the system prompt and tool list are not compared, unless they are in the message list. A call that shrank the prompt by more than 5% also counts as `context changed`.
- Columns shorter than `pause_m` are for information only: the guard does not act there.
- The columns show what was observed, not provider cache lifetimes. One `pause_m` covers every model, unless the model has its own line.
- The table shows from 5 measurements in all. Before that, it says `(3 measurements so far, need at least 5)`.
- `/cacheguard all` shows every model with at least one counted pause, not only the top 10. Models with none are left out.


## The block text and the result line

The basics are in the [README](../README.md#using-it). More:

**When it acts**

- The chat paused at least `pause_m` and holds at least `size_k` tokens.
- After a model change, no pause is needed.

**The block text**

- The message is not sent. The reply starts with `[CACHEGUARD] BLOCKED · `. Needs Hermes 0.22.6 or newer.
- By default the text has 3 lines:

      [CACHEGUARD] BLOCKED · 13:05 · 12 minutes pause 🕐 · ~45,000 tokens may be rebilled · lost 3 of 4 similar pauses
      To send it anyway: /retry, or send it again.
      Or start /new and paste it. Session ID: …

- `13:05` is the time of the block, on the clock of the machine running Hermes (a Telegram user in another time zone sees that machine's time).
- After a model change the first line is `[CACHEGUARD] BLOCKED · 13:05 · model changed a → b · ~45,000 tokens may be rebilled`.
- With `tiny_block on` it is one line: `[CACHEGUARD] BLOCKED · 13:05 · 12m 🕐 · ~45kt · 3/4` (`3/4`: lost 3 of 4 similar pauses). Start with the default; once you know the plugin, switch tiny on.
- The model is compared by name only (the hook gets no provider). The provider comes from the model stored for the chat.

**Sending a blocked message**

- Use `/retry`, or send the same text again. That one resend goes out with no new check, however long you wait, unless Hermes restarts in between.
- In Telegram and other chats, `/retry` is a tappable command.
- In the web UI, `/retry` shows `[Message withheld: …]` as your message. The model still gets the text you typed.
- After a message is sent, the pause clock restarts: no new block until the next pause.

**Images**

- A message with an image is only warned, because Hermes' `/retry` refuses images and the up arrow drops them.
- With `block_images on` it is blocked too: attach the image again and send it again.
- If Hermes first turns images into a text description (`image_input_mode: text`, models without vision), cacheguard sees only text and treats the message as text.

**Runs with no screen**

- `hermes chat -q` has no screen to read the reply, so `block` acts as `warn`.
- `-Q`, `--oneshot` and `hermes -z` runs are left alone like background calls: no block, no line. A `-Q` run may still be counted in the stats.

**The result line**

- When cacheguard cannot tell whether the cache held, the line says `cache unknown` and why, for example `cache unknown, needs 1 more cache hit`. This happens on the first call of a chat, with a model that does not report its cache, or after a call that read nothing from the cache before the model showed 2 cache hits…
- After a model change, a loss counts up to N tokens rebilled: the count includes your new message.

## What the model sees

The model never sees cacheguard's lines.

**Result lines and notices**

- They are posted as short chat notices, outside the conversation.
- They start with `CGUARD` and the time, for example `CGUARD 13:05 23k reused, 1 min` or `CGUARD 13:05 WARN cache lost, 23k rebilled, 10 min`. They are short because the TUI status line cuts text at about 45 characters.
- Where they show: classic CLI, a dim line at the top of the reply; TUI, the status line at the bottom; Desktop and Web UI, a notification; Telegram, a separate bot message.

**BLOCKED replies**

- Hermes saves them in the chat as real replies, as if the model wrote them. You still see them.

**Good to know**

- Installing, updating, or turning cacheguard off or on can change what is sent only in a chat that holds a BLOCKED reply or a `[Message withheld: …]` line: its next call may lose the cache once. Other chats keep it.
- Hermes' chat summaries (compression) may still mention these lines.
- If removing them fails, the call is sent unchanged.

## Unattended runs

Cron, kanban, subagents, the curator, the API server and webhooks have nobody to read the reply. cacheguard does nothing there: no block, no line, no measurement.

Bot Chat is left alone when it runs as a one-shot (`-c NAME -Q`). A Bot Chat reached through a messaging chat is treated like any gateway chat: it follows the `gateway` mode (default `off`).

## After installing or updating

If a Hermes gateway is running, even a terminal chat runs its plugins inside the gateway. The gateway does not reload plugin code by itself, so restart it after you install, update, enable or disable cacheguard. A new chat alone is not enough. The commands for each kind of gateway are in the README: [Restart the gateway](../README.md#restart-the-gateway).

The gateway needs one to a few minutes to start. Meanwhile a chat may show a black screen or queue your messages. An open chat reconnects by itself.

## Several profiles

Each profile has its own plugin, settings and history. Put `-p <profile>` after `hermes`:

    hermes -p <profile> plugins install https://github.com/AIverflow/hermes-cacheguard.git --enable
    hermes -p <profile> plugins update cacheguard
    hermes -p <profile> plugins disable cacheguard

To set it up, type `/cacheguard setup all block 10 20` in a chat started with `hermes -p <profile>`.

## Health

If cacheguard fails, your message is sent.

- `/cacheguard` shows a `health: degraded` line, with the details, when something is wrong. No such line means all is fine.
- Hermes changes often, so at startup cacheguard checks that the Hermes parts it uses still exist.
- If one moved, cacheguard says so in one short sentence, once per chat. For example:
  - `cacheguard cannot block here: it only warns.`
  - `cacheguard is off: it could not start.`
- It only reports: what cacheguard does stays the same.

## Known limits

- Tested on Linux only. Windows is untested: testers welcome, please open an issue.
- Tested in the terminal, desktop app, web UI and Telegram. Not tested: editor, other messaging platforms.
- A warning is a guess, not proof that the cache expired.
- A model line is chosen by model name and the chat's last provider: after a provider switch with the same model name, the first block check uses the old provider's line.
- cacheguard counts tokens, not dollars.
- Calls that bypass the Hermes middleware (e.g. `codex_app_server`) are not seen.

## For maintainers: daily Hermes check

`.github/workflows/hermes-compat.yml` runs every day (and on demand). It installs Hermes at the version in `requires_hermes`, at the latest release and at `main`, on Ubuntu, Windows and macOS, then runs `hermes plugins validate`, `hermes plugins doctor --ci`, `scripts/hermes_compat.py` (the `/cacheguard` health checks) and the tests. A failing cell does not fail the run: see the table in the run summary. A Telegram alert goes out only when a cell changes (pass to fail or back, a new cell, or a new Hermes release). The last state is kept in the `hermes-compat-state` artifact. Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`; if one is missing, no alert is sent. GitHub pauses scheduled workflows after 60 days without repo activity: re-enable it in the Actions tab.
