# Cacheguard guide

The basics are in the [README](../README.md). This page covers the rest.


## Get more from your cache

How to make the cache last longer, per provider, and what to do once it is gone. Measured on a few runs: it may change.

Anthropic (Claude):

- Default cache: 5 minutes (tested). The default `pause_m` of 10 is longer, so cacheguard misses these losses.
- If you often pause more than 5 minutes: a 1-hour cache. Set `prompt_caching.cache_ttl: 1h` in the Hermes config, then `/cacheguard set pause_m 55`. Tested: still kept after 30 minutes. Writing to this cache costs 2x the input price instead of 1.25x (Anthropic docs).
- Or keep the 5-minute cache and set `pause_m` below it: `/cacheguard set pause_m 4`.

OpenRouter:

- After 10 minutes without a message, it may switch provider, and the new one has no cache (OpenRouter docs). Pin one provider per model in the Hermes config:

```yaml
provider_routing:
  models:
    deepseek/deepseek-v4.1-flash:
      only:
        - deepinfra
```

DeepSeek:

- No fixed cache time (DeepSeek docs). Tested once: kept after 2 hours, lost after 2.5 hours.

Once a large chat has gone cold, it often costs less to start over: type `/new`, give the new chat the old session ID, and ask it to read the end of that session. In the CLI you can also run `/cacheguard export 5 5` first, then paste the saved path into the new chat.

Sources: [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching), [OpenRouter prompt caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching), [DeepSeek context caching](https://api-docs.deepseek.com/guides/kv_cache/).


## Groups

Each chat belongs to one group, by the platform Hermes reports.

| Group | Covers |
|---|---|
| `cli` | terminal, editor |
| `desktop` | desktop app, web UI |
| `gateway` | Telegram, Discord, Slack and every other messaging platform, including platforms the plugin does not know |

Unattended calls (cron, kanban, subagents, API server, webhooks, Bot Chat) are excluded. This is not a group you can set.

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
| `pause_m` | number, 1 or more | 10 | Pause length that counts as a pause, in minutes |
| `size_k` | number, 0 to 2000, in thousands of tokens (`20` = 20k) | 20 | Smallest prompt to act on |

Other commands:

| Command | What it does |
|---|---|
| `/cacheguard all` | Table with every recorded model |
| `/cacheguard <model>` | Only models whose name contains the text |
| `/cacheguard export <first> <last>` | CLI only. Saves this chat's messages to a file: `export 5 5` = first 5 and last 5, `export all` = everything. Use it before `/new` |
| `/cacheguard debug` | Recent raw records |
| `/cacheguard reset` | Delete measurements and session tracking. Settings and exported chats are kept |

In a terminal, `hermes cacheguard` shows the same table and takes `help`, `setup` and `set`. `setup` and `set` write to the profile's `config.yaml`. The plugin must be enabled.

## Model change

A cache belongs to one model. If you switch model or provider in a large chat, the box says `Model changed` (in `warn`, the result line ends `, model changed`): the new model has no cache yet, so the whole chat is billed again. No pause is needed. Like the pause box, it shows only when the group is `warn` or `block` and the chat is over the size limit.

If you switch back quickly (A → B → A), A's cache may still be there. The result line shows what was really reused.


## The estimate and the table

cacheguard records each call (tokens sent, tokens read from the cache) in `<hermes home>/cacheguard/`, one history per profile. Nothing leaves your machine.

It also keeps a small file for each of the last 200 chats. A chat it has no file for gets no box until its first reply: a chat whose file was dropped, a chat started before cacheguard was on, or after `/cacheguard reset`.

A call lost the cache when at least 30% of the previous prompt was not read from it. The box says `lost 4 of 10 similar pauses`: how often that happened before, for the same model after a similar pause. It shows after 10 such calls.

`/cacheguard` shows the same data as a table (some columns left out; the pause columns go up to over 2h01, `suggested` is last):

    model                   1-6m  6-21m  21-41m  suggested
    openai-codex/gpt-6-sol  0/14   2/17     2/4        16m

- `2/17`: 2 of 17 calls lost the cache. Every cell is `lost/calls`, no percent. The box shows a count only from 10 calls in that pause length.
- `suggested`: the longest pause where the cache was lost in 10% of calls or fewer, on a curve that never goes down as the pause grows (isotonic fit). `none` when even the shortest pauses lose more. Shown from 20 measurements of that model, otherwise `—`.
- `+`: no loss seen beyond that pause; the cache may last longer.
- Not counted: pauses under 1 minute, the first call of a chat, model switches, calls where the chat itself changed (`context changed`), background calls.
- `context changed`: the start of the chat was not what the last call sent, for example Hermes edited an old message. cacheguard compares a fingerprint of the messages, kept in the chat's file (the messages themselves are not stored, and nothing is sent to the provider). Cache markers are ignored; the system prompt and tool list are not compared, unless they are in the message list. A call that shrank the prompt by more than 5% also counts as `context changed`.
- Columns shorter than `pause_m` are for information only: the guard does not act there.
- The columns show what was observed, not provider cache lifetimes. One `pause_m` covers every model.
- `/cacheguard all` also shows models with no data yet.


## The box and the result line

The basics are in the [README](../README.md#using-it). A few more things:

- cacheguard acts on your message when the chat paused longer than `pause_m` and is at least `size_k` tokens. After a model change, no pause is needed.
- After you send, the box stays quiet in that chat for `pause_m` minutes, so it does not ask at every message.
- With no screen to show the box (`hermes chat -Q`, `--oneshot`, `hermes -z`), you only get the result line.
- When cacheguard cannot tell whether the cache held (first call of a chat, a model that does not report its cache…), the line says `cache unknown` and why.
- After a model change, a loss counts up to N tokens rebilled: the count includes your new message.


## What the model sees

The result lines and one-off notices are posted as short chat notices, outside the conversation the model sees. They start with `CGUARD`, for example `CGUARD 23k reused, 1 min` or `CGUARD WARN cache lost, 23k rebilled, 10 min`: the TUI status line cuts text at about 45 characters. Where they show: classic CLI, a dim line at the top of the reply; TUI, the status line at the bottom; Desktop and Web UI, a notification; Telegram, a separate bot message. Hermes older than 0.22.3 (0.22.2 builds from before 2026-10-18) cannot post notices: cacheguard then shows no result lines.

The `BLOCKED` reply is a real reply: Hermes saves it in the chat as if the model wrote it. Sent back, it would cost tokens and confuse the model. So before each call, cacheguard removes it from what is sent, with the cancelled message before it. You still see them in the chat. Older cacheguard versions added their result lines to replies; chats that already hold such lines are cleaned the same way.

- Only lines starting with `[CACHEGUARD]` are removed, always the same way, so the cache holds.
- Installing, updating, or turning cacheguard off or on can change what is sent only in a chat that holds cacheguard lines (a `BLOCKED` reply, or result lines from an older version): its next call may lose the cache once. Other chats keep it.
- Hermes' chat summaries (compression) may still mention these lines.
- If removing them fails, the call is sent unchanged.


## Unattended runs

Cron, kanban, subagents, the API server, webhooks and Bot Chat have nobody to answer or read. cacheguard does nothing there: no box, no line, no measurement.

Bot Chat is left alone for now, even when reached through Telegram.

## After installing or updating

If a Hermes gateway is running, even a terminal chat runs its plugins inside the gateway. The gateway does not reload plugin code by itself, so restart it after you install, update, enable or disable cacheguard. A new chat alone is not enough. The commands for each kind of gateway are in the README: [Restart the gateway](../README.md#restart-the-gateway).

The gateway needs one to a few minutes to start. Meanwhile a chat may show a black screen or queue your messages. An open chat reconnects by itself.

## Several profiles

Each profile has its own plugin, settings and history. Put `-p <profile>` after `hermes`:

    hermes -p <profile> plugins install https://github.com/AIverflow/hermes-cacheguard.git --enable
    hermes -p <profile> plugins update cacheguard
    hermes -p <profile> cacheguard setup all block 10 20
    hermes -p <profile> plugins disable cacheguard

## Health

If cacheguard fails, your message is sent. Only a Cancel you already gave stays "not sent".

- If the box itself fails, the message is sent and cacheguard only warns until Hermes restarts. One line says so.
- `/cacheguard` shows a `health: degraded` line when something is wrong. No such line means all is fine.
- Hermes changes often. At startup, cacheguard checks the Hermes parts it uses, and how it calls them (the arguments, and plain or async). If one moved, it prints one short sentence, once, and `/cacheguard` gives the details (for a changed call, the function and what changed). It only reports: what cacheguard does stays the same. For example:
  - `cacheguard cannot show the box here: it only warns.`
  - `cacheguard lines may be sent to the model.`
  - `cacheguard is off: it could not start.`


## Known limits

- Tested on Linux only. Windows is untested: testers welcome, please open an issue.
- Tested in the terminal, desktop app, web UI and Telegram. Not tested: editor, other messaging platforms.
- A warning is a guess, not proof that the cache expired.
- cacheguard counts tokens, not dollars.
- Calls that bypass the Hermes middleware (e.g. `codex_app_server`) are not seen.
