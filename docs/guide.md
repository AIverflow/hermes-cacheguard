# cacheguard guide

The basics are in the [README](../README.md). This page covers the rest.

The name is `cacheguard` everywhere: chat command `/cacheguard`, terminal command `hermes cacheguard`, plugin folder, config key and data folder.

## Groups

Each chat belongs to one group, by the platform Hermes reports.

| Group | Covers |
|---|---|
| `cli` | terminal, editor |
| `desktop` | desktop app, web UI |
| `gateway` | Telegram, Discord, Slack and every other chat app, including platforms the plugin does not know |

Unattended calls (cron, kanban, subagents, API server, webhooks, Bot Chat) are excluded. This is not a group you can set.

Each group has its own mode (`off`, `warn`, `block`). Pause and size are shared.

`setup all …` sets the three groups, even one that is off. `setup <group> …` sets that group's mode, but pause and size still apply to all. If `desktop` is unset, it follows `cli` and changes too. To change one group's mode and nothing else: `/cacheguard set gateway block`.

## Settings

Set them with `/cacheguard set <name> <value>`, or in `<hermes home>/config.yaml` under `plugins.entries.cacheguard.settings` (see [config-example.yaml](config-example.yaml)). Quote `off` and `on` in YAML (`gateway: "off"`), or YAML reads them as `false` and `true`. A top-level `cacheguard:` section also works; `plugins.entries` wins.

| config.yaml key | `/cacheguard set` name | Values | Default | Meaning |
|---|---|---|---|---|
| `enabled` | none | true, false | true | Full stop: no box, no line, no measuring. Group settings stay as they are. `/cacheguard off` only turns the groups off and keeps measuring |
| `cli` | `cli` | off, warn, block | warn | Terminal, editor |
| `desktop` | `desktop` | off, warn, block | unset (follows `cli`) | Desktop app, web UI |
| `gateway` | `gateway` | off, warn, block | off | Chat apps |
| `model_change` | `model_change` | off, warn, block | the group's mode | Switching model or provider |
| `show_model` | `show_model` | on, off | off | Name the model in the box and the result line |
| `display_on_success` | `display_on_success` | on, off | off | Show the result line when the cache was kept |
| `min_gap_seconds` | `minutes` | `minutes`: 1 or more. In YAML: seconds | 600 (10 minutes) | Pause length that counts as a pause |
| `min_context_tokens` | `tokens` | `tokens`: thousands, max 2000 (`20` = 20k). In YAML: tokens | 20000 | Smallest prompt to act on |
| `max_observations` | `max_observations` | whole number, 1000 or more | 10000 | History rows kept |

One pause setting covers every model. The table columns show what was observed. They are not provider cache lifetimes.

Other commands:

| Command | What it does |
|---|---|
| `/cacheguard all` | Table with every recorded model |
| `/cacheguard <model>` | Only models whose name contains the text. No match: "No recorded model matches X. Use /cacheguard all to list recorded models." |
| `/cacheguard export <first> <last>` | CLI only. Saves the first and last messages of this chat, e.g. `export 5 5`. `export all` saves everything. No model call. Run it before `/new`. The file is plain text (only you can read it), and `reset` keeps it |
| `/cacheguard debug` | Recent raw records |
| `/cacheguard reset` | Delete measurements and session tracking. Settings and exported chats are kept |

In a terminal, `hermes cacheguard setup …` writes straight to the profile's `config.yaml`. It works only when the plugin is enabled in that profile.

## Model change

If you switch model or provider, the box says `Model changed` and that the tokens "may reload". A cache belongs to one model.

- It can fire without a pause.
- It follows the group's mode. It cannot override a group set to `off`.
- After a quick switch back (A to B to A), A's cache may still be warm, so the box can overstate the loss. The result line shows what was really reused.
- A cancelled or failed request is not recorded. Switching back after one compares with the last model that answered.

## The estimate and the table

Every call is measured (tokens sent, tokens read from the cache) and saved in `<hermes home>/cacheguard/`. Nothing leaves your machine, and cacheguard makes no network calls. Each profile has its own history.

A call counts as a call without cache when at least 30% of the previous prompt was not read from the cache. Calls are compared only within the same session and model. A zero cache read counts only after three earlier calls with cache reads for that model.

The chance of loss (`chance of loss 40%`) is the share of such calls among past calls of the same model with a pause in the same range. It appears from 10 such calls, so a new install shows none. It describes your history, not this request or a probability.

The table, headed "Cache lost, by pause length":

    model                      1-6m       6-21m  21-41m
    openai-codex/gpt-6-sol  0% (14)  11.8% (17)     2/4

- Pause ranges: 1-6, 6-21, 21-41, 41-61, 61-91, 91-121 minutes, and over 2h01. Edges sit one minute past a round time, because the measured pause runs a little longer than the time the provider counts.
- A cell is `percentage (calls)`. `17.6% (17)` means 3 of 17 calls lost the cache.
- Under 10 calls, a cell shows `lost/calls`, e.g. `1/3`. `—` means no usable calls.
- Models with no usable calls are hidden. `/cacheguard all` shows them.
- Pauses under 1 minute are not counted. Neither are the first call of a chat, a context rewrite, a model switch, or background calls.
- The guard acts only after `min_gap_seconds`. Shorter columns are for information.
- On Markdown surfaces (Telegram, Discord, Slack, Matrix, Mattermost, web UI) the table is sent in a code fence so columns line up.

Background tasks (`/background`, subagents) have their own task ids. They do not change the chat's history. One exception: if a background call on the same model reads at least 70% of the chat's last prompt from the cache, the cache was kept warm, and the pause clock restarts.

## The box and the result line

- Chat apps show the box as a message with buttons. If it cannot reach you, the `BLOCKED` line ends `… and the prompt could not be delivered`.
- Cancel, no answer, or a timeout (Hermes' `approvals.timeout`, 5 minutes by default) sends nothing. A cancelled message stays in the chat history, but the model does not get it. The `BLOCKED` line says `(cancelled)` or `… and 300s timeout`, then the recovery step: `Send it again, or start /new and paste it. Session ID: <id>`.
- After you allow a send, no new box for the same session and model until the pause window is crossed again, even if the provider call fails.
- The desktop app, web UI and editor show the box the same way as chat apps.
- With no screen (`hermes chat -Q`, `--oneshot`, `hermes -z`), `block` acts like `warn`: the message is sent and the line starts with `WARN ·`. The same happens when this Hermes has no way to ask. Resuming a chat that way (`hermes chat -c NAME -Q`) is guarded with a warning, never a box.
- Cacheguard lines stay in the chat but are not sent to the model. See [What the model sees](#what-the-model-sees).
- Result line:
  - `OK · 41,200 tokens reused after a 12-minute pause`: the cache held.
  - `WARN · cache lost · 41,200 tokens reloaded`: part of the prompt was reloaded.
  - `WARN · Cache status unknown · <reason>`: the call cannot be judged. Reasons: `first call, nothing to compare yet`; `not enough earlier cache hits to judge`; `this model does not report a cache` (said once per model, then nothing more); `model changed`, `context changed`, `background task`; `usage unavailable`, `older measurement`, `provider or API unknown`.
  - After a model switch the line ends `· model changed A → B`, and a loss reads `cache lost · up to N tokens reloaded`: the count includes your new message.
- `OK ·` and `WARN ·` appear when no box was shown. After a box the line has no tag.
- With `display_on_success` off (default), the line is hidden when the cache was kept (`OK`, with or without tag). `cache lost` lines and "Cache status unknown" lines always show, because a first call is not a success (except `this model does not report a cache`, shown once per model).
- The line goes at the end of the reply, because Hermes streaming only accepts appended text. With `warn`, a turn that runs for minutes gets its line only at the end. In chat apps `warn` cannot show before sending either. Use `block` to know before anything is sent.
- If the model copies an earlier result line into its reply, cacheguard removes it first. The CLI then prints the final reply once more with a short notice. This is rare.
- With `show_model` on, a line such as `using openrouter deepseek-v4.1-flash` is added under the box, the result line and the `BLOCKED` text.

## What the model sees

Hermes stores cacheguard's lines as if the model wrote them: the result line is part of the reply, and a `BLOCKED` text is stored as the reply. The message you cancelled is stored too. Hermes has no way to show a line only to you, or to drop a message before it is stored. Sent back to the model, these lines cost tokens, and the model may think it wrote them.

So, before each provider call, cacheguard cleans a copy of what is sent:

- It removes the result line at the end of each reply (and the `using …` line under it).
- It removes each `BLOCKED` reply and the message just before it, since that message was never sent.

What stays the same:

- You still see every line in the chat, and they stay in the session history and in `session_search`.
- Only cacheguard's own lines go: a reply or message is changed only when it matches cacheguard's text exactly (prefix `[CACHEGUARD]`). A message you type that starts with the prefix is kept.
- The same chat is always cleaned the same way, so the start of the prompt does not change from one call to the next and the cache still holds.

Limits:

- Chats open during the update get one call without cache, because the start of the prompt changes once.
- Hermes' own chat summaries (compression) may still mention these lines.
- If the cleaning fails, the request is sent unchanged. If the Hermes part it relies on moved, `/cacheguard` says `cacheguard lines may be sent to the model.` under health.

Checked live in the terminal (classic and full-screen), the desktop app and Telegram.

## Unattended runs

Cron, kanban, subagents, the API server, webhooks and Bot Chat have nobody to answer or read. cacheguard does nothing there: no box, no line, no measurement.

Bot Chat is left alone for now, even when reached through Telegram.

## Several profiles

Each profile has its own plugin, settings and history. Install with `hermes -p <profile> plugins install https://github.com/AIverflow/hermes-cacheguard.git --enable`. Update with `hermes -p <profile> plugins update cacheguard`. Set up with `hermes -p <profile> cacheguard setup all block 10 20`.

## Health

If the plugin fails at any point, your message is still sent. If the Send/Cancel box itself fails, the message is sent, the box is off until Hermes restarts (`block` acts as `warn`), and cacheguard prints one plain line: `the Send/Cancel box failed, so messages now go out with a warning until Hermes restarts. Details: /cacheguard show`. A Cancel you already gave stays "not sent". An answer cacheguard cannot read prints `unreadable answer in the Send/Cancel box: message not sent`.

- `/cacheguard` prints a `health: degraded` line only when something is wrong. No such line = it works.
- At startup cacheguard checks the Hermes parts it relies on. If one moved, it prints one plain sentence per problem. `/cacheguard` then lists the problem under `health: degraded`, with the technical name and detail.
- `cacheguard cannot show the box here: it only warns.` means this Hermes has no way to ask, or it changed. The message is sent with a `WARN` line instead of asking.
- `typed answers in the Send/Cancel box may not work: use the buttons.` means this Hermes no longer has its answer reader. Only the button answers and `send` send. An answer cacheguard cannot read never sends the message and prints one line.
- Other sentences: `cacheguard cannot read the chat history: some features are off.`, `cacheguard cannot read cache numbers: results may be missing.`, `cacheguard may count background calls as yours.`, `cacheguard may treat cron and kanban runs as chats.`, `cacheguard may treat Bot Chat as a normal chat.`, `with -Q and -z, cacheguard may ask where it should only warn.`, `Bot Chat and export in chat apps may not find this chat.`, `the box timeout may differ from Hermes.`, `cacheguard is off: it could not start.` Each means a Hermes part moved. Except for the last one, the message is still sent.
- The Send/Cancel box waits as long as Hermes does (`approvals.timeout`, capped the way Hermes caps it).
- Config or data errors are listed there too.

## Keeping the cache warm

Early findings. "Tested" means we measured it. "Docs" means provider documentation only.

Anthropic (Claude):

- Hermes default cache time is 5 minutes. Tested: gone after 5 minutes.
- Longer cache: set `prompt_caching.cache_ttl: 1h` in the Hermes config. Tested: Sonnet 5.5 still hit the cache after about 8 minutes and after 30 minutes.
- Docs: writing to the 1-hour cache costs 2x the input price, instead of 1.25x. We have not measured the real extra cost.
- With the default 5-minute cache, use a pause under 5 minutes: `/cacheguard setup all block 4 20` (4 minutes, prompts of 20k tokens or more).
- With a 1-hour cache, set the pause a bit under the hour, e.g. `/cacheguard set minutes 55`.

OpenRouter:

- Docs: you stay on the same provider only if you send again within 10 minutes. After that it may switch, and a new provider starts without your cache.
- Pin one provider per model in the Hermes config:

```yaml
provider_routing:
  models:
    deepseek/deepseek-v4.1-flash:
      only:
        - deepinfra
```

DeepSeek:

- Tested once (V4.1 Flash via OpenRouter, pinned to DeepInfra): warm after 2 hours, gone after 2.5 hours. Needs more tests.
- Docs: no fixed cache time. Unused entries are usually cleared within hours to days.

For a pause longer than the cache time: `/cacheguard export 5 5` (CLI only), then `/new`. Paste the saved path into the new chat.

Sources: [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching), [OpenRouter prompt caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching), [DeepSeek context caching](https://api-docs.deepseek.com/guides/kv_cache/).

## Known limits

- Tested on Linux only. Windows is experimental and untested. Testers are welcome: please open an issue.
- Tested: terminal (classic and full-screen), desktop app, web UI, Telegram. Not tested: editor, other chat apps.
- A warning is a guess. It does not prove the cache expired.
- Calls that bypass the Hermes middleware (e.g. codex_app_server) are not covered. A provider error before a response leaves no record.
- Prompt size is estimated at about four characters per token, or the last recorded size if larger.
- Equal-size context rewrites cannot be detected, and provider prefix caps can make a loss partial.
- cacheguard counts tokens, not dollars.

## Uninstall

    hermes plugins remove cacheguard

For another profile: `hermes -p <profile> plugins remove cacheguard`. Then delete `<hermes home>/cacheguard/` if you want the history gone.
