"""cacheguard — warn or block before sending a large context after a pause.

The first API call of each turn is guarded using context-size and idle-time
thresholds. Cache availability is unknown before the provider call; the block text
shows only how often the cache was lost after similar pauses before. In block mode,
the pre_llm_call hook stops the message; the user sends it with /retry or sends it again.

Works in the Hermes CLI and in the gateway.
Bot Chat is left alone when it runs as a one-shot (`-c NAME -Q`). In a messaging chat it is a normal gateway chat.

The llm_execution middleware reads normalized usage from every response, with
no extra provider calls, and keeps a journal row only for a chat call after a
pause of 1 minute or more (no session ID in it). /cacheguard shows historical call and cold counts
per provider/model and idle range. Cold is a low-reuse classification, not
proof that a cache expired.

Observations live in <hermes home>/cacheguard/observations.jsonl.
Plugin failures allow the provider call to proceed.
"""

from __future__ import annotations

from typing import Any

# Each module name below is the one home of the names it defines: other modules read them as `module.name`.
from . import commands, config, confirm, guard, health, measure, storage  # noqa: F401


def register(ctx: Any) -> None:
    config._CTX = ctx
    try:
        health._run_health()
    except Exception:
        pass
    try:
        ctx.register_middleware("llm_execution", guard._on_llm_execution)
    except Exception as exc:
        health._H["checks"]["middleware"] = (False, f"llm_execution: {type(exc).__name__}: {exc}")
        health._notice(health._HEALTH_EFFECTS["middleware"])
    try:
        ctx.register_middleware("llm_request", guard._on_llm_request)
    except Exception as exc:
        health._H["checks"]["llm_request"] = (False, f"{type(exc).__name__}: {exc}")
    try:
        ctx.register_hook("transform_llm_output", guard._transform_llm_output)
    except Exception as exc:
        health._H["checks"]["transform_llm_output"] = (False, f"{type(exc).__name__}: {exc}")
    try:
        ctx.register_hook("pre_llm_call", guard._on_pre_llm_call)
    except Exception as exc:
        health._H["checks"]["pre_llm_call"] = (False, f"{type(exc).__name__}: {exc}")
    try:
        ctx.register_command(
            "cacheguard",
            commands._cmd_entry,
            description="Show cacheguard status. 'setup' and 'set' change settings; 'reset' clears saved history.",
            args_hint="[help | all | <model> | setup <all|cli|desktop|gateway> <mode> [pause_m] [size_k] | set <name> <value> | reset]",
        )
    except Exception:
        pass
