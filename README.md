# cacheguard

A Hermes plugin for prompt-cache cost. After a pause, the provider may have dropped your cached chat, and the next large message is billed in full. cacheguard can ask before sending it (`block`) or tell you afterwards what was reused or reloaded (`warn`).

## Safe by default

- cacheguard never calls the model itself, and nothing leaves your machine.
- If the plugin fails at any point, your message is still sent. If the Send/Cancel box itself fails, cacheguard only warns until Hermes restarts.

## Install

Needs Hermes 0.21.5 or newer:

    hermes plugins install https://github.com/AIverflow/hermes-cacheguard.git --enable

Start a new chat (chat apps: also `hermes gateway restart`). Type `/cacheguard` to check. A new install says `(no observations yet)`. If there is no `health: degraded` line, it works.

For another profile, put `-p <profile>` after `hermes`: `hermes -p <profile> plugins install …`.

Update and remove:

    hermes plugins update cacheguard
    hermes plugins remove cacheguard

Manual install: clone into the plugin folder, which must be named `cacheguard`, then enable it. To update it later with Hermes, run `hermes plugins adopt cacheguard` once.

    git clone https://github.com/AIverflow/hermes-cacheguard.git ~/.hermes/plugins/cacheguard
    hermes plugins enable cacheguard

Use `/cacheguard` in a chat and `hermes cacheguard` in a terminal. Both take the same commands.

## Quick setup

Defaults: terminal `warn`, chat apps `off`, desktop follows the terminal. A pause is 10 minutes and a large prompt is 20k tokens.

    /cacheguard setup all block 10 20

Format: `setup <group> <mode> [minutes] [k tokens]`.

- Modes: `off`, `warn` (report afterwards), `block` (ask first).
- Groups: `cli` (terminal, editor), `desktop` (desktop app, web UI), `gateway` (chat apps), `all`.

Claude keeps its cache 5 minutes by default, but the default pause is 10. For Claude, use:

    /cacheguard setup all block 4 20

That means: all groups ask first (`block`) after a pause of 4 minutes, for prompts of 20 thousand tokens or more.

## What you see

In `block` mode, a box appears before a large message that follows a pause:

    [CACHEGUARD] Possible cache cost

    Paused 12 minutes · ~48,000 tokens may reload · chance of loss 40%

- **Send message**: it goes out now.
- **Cancel**, no answer, or an answer cacheguard cannot read (it prints one line saying so): nothing is sent, and a `BLOCKED` line tells you how to continue in a new chat.
- No answer within the Hermes approval time (5 minutes by default) counts as Cancel. The cancelled text stays visible in the chat, but the model never gets it.

Button names depend on the screen: the full-screen terminal shows `Allow once` and `Deny`.

After the reply to a large message that followed a pause, one line says what happened:

    [CACHEGUARD] OK · 41,200 tokens reused after a 12-minute pause
    [CACHEGUARD] WARN · cache lost · 41,200 tokens reloaded

`chance of loss` (in the box) is how often your past chats lost the cache after a similar pause. It shows after about 10 similar calls. Small messages and short pauses stay silent.

Lines for a kept cache are hidden by default; `/cacheguard set display_on_success on` shows them.

The lines are not sent back to the model: [details](docs/guide.md#what-the-model-sees).

## Commands

| Command | What it does |
|---|---|
| `/cacheguard` | Table: how often the cache was lost, by pause length |
| `/cacheguard setup <group> <mode> [minutes] [k tokens]` | Set mode, pause and size (give both or neither) |
| `/cacheguard set <name> <value>` | Change one setting |
| `/cacheguard off` | All groups off: no box, no line. Measuring continues |
| `/cacheguard help` | Every command |

To stop everything, including measuring, set `enabled: false` in `config.yaml` (see the [guide](docs/guide.md#settings)).

## More

The [guide](docs/guide.md) covers every setting, the estimate, unattended runs, profiles and uninstall.

## License

MIT
