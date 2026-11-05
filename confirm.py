"""The confirmation box (a gateway question), its answers, and the "not sent" reply."""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from typing import Any

from . import config, guard, health, measure, storage


def _gateway_approval() -> Any:
    """Only a gateway turn with a registered human notification path can hold."""
    try:
        from tools.approval import _gateway_notify_cb
        from tools.approval_context import get_current_session_key

        key = get_current_session_key(default="")
        notify = _gateway_notify_cb(key) if key else None
        return (key, notify) if notify is not None else None
    except (ImportError, AttributeError):
        return None


def _gateway_confirmation(payload: dict[str, Any], command: str, deny_text: Any, model: str,
                          box: dict[str, Any]) -> Any:
    context = _gateway_approval()
    if context is None:
        return None  # nobody to ask: send
    from tools.approval_gateway_wait import _await_gateway_decision

    key, notify = context
    pattern = "cacheguard:" + guard._domain_key(payload)
    box["shown"] = True
    try:
        decision = _await_gateway_decision(key, notify, {
            "command": command, "description": "cacheguard", "pattern_key": pattern,
            "pattern_keys": [pattern], "allow_session": False, "allow_permanent": False,
        })
        if not isinstance(decision, dict) or not ("resolved" in decision or decision.get("notify_failed")):
            raise ValueError("unexpected box answer")  # Hermes changed shape: a box failure, not a Cancel
        resolved, choice = decision.get("resolved"), decision.get("choice")
        if decision.get("notify_failed"):
            outcome = "undelivered"
        elif resolved is True and choice in ("once", "session", "always"):
            return None
        elif resolved is True and choice in ("deny", None):  # None: prompt withdrawn, nobody answered
            outcome = "cancelled"
        elif resolved is False and choice is None:
            outcome = "timeout"
        else:
            raise ValueError("unknown box answer value")  # Hermes changed a value: a box failure
    except Exception as exc:
        _box_broke(exc)  # the box failed: send, and stop asking until restart
        return None
    box["denied"] = outcome
    return _denied_response(model, deny_text, outcome, payload.get("api_mode"))


def _single_query() -> bool:
    """Hermes' own marker for a run with no screen (`chat -q`): nobody can answer a prompt."""
    try:
        from tools.approval_context import _is_single_query_approval_context

        return bool(_is_single_query_approval_context())
    except Exception:
        return os.environ.get("HERMES_SINGLE_QUERY_SESSION", "").strip().lower() in ("1", "true", "yes", "on")


def _pause_text(gap_s: float) -> str:
    count, unit = config._pause_parts(gap_s)
    return f"{count} {unit}{'' if count == 1 else 's'}"


def _cache_cost_text(size: int, gap_s: float, cold: tuple[int, int] | None = None) -> str:
    """The confirm box text. cold: (lost, calls) of past calls after this pause, if known."""
    return (f"{config._PREFIX} Possible cache cost\n\n"
            f"Paused {_pause_text(gap_s)} · ~{size:,} tokens may be rebilled"
            + (f" · lost {cold[0]} of {cold[1]} similar pauses" if cold is not None else ""))


def _cold_counts(model: str, gap_s: float) -> tuple[int, int] | None:
    """(lost, calls): past calls of this model that found a cold cache after a similar pause.

    None until the matching pause bucket holds _MIN_CHANCE_CALLS measurements.
    """
    try:
        rows = storage._load_observations()
        cells = measure._cold_cells(measure.evaluate_observations(rows)).get(model)
        cell = cells[config._bucket_index(float(gap_s), measure._GAP_EDGES)] if cells else None
        if cell and cell["calls"] >= measure._MIN_CHANCE_CALLS:
            return cell["cold"], cell["calls"]
    except Exception:
        pass
    return None


_NOT_SENT_TEXT = config._BLOCKED_PREFIX + "message not sent"
_BOX_OFF_TEXT = ("the cacheguard box failed, so messages now go out with a warning "
                 "until Hermes restarts. Details: /cacheguard")


def _box_broke(exc: BaseException) -> None:
    """The box call failed: switch the box off for this process and say so once."""
    health._H["box_failed"] = type(exc).__name__
    health._notice(_BOX_OFF_TEXT)


def _denied_response(model: str, deny_text: Any, outcome: str, api_mode: Any) -> Any:
    """The "not sent" reply for a known no-send answer. None if even that fails: the caller sends."""
    try:
        try:
            text = _denied(deny_text, outcome)
        except Exception:
            text = _NOT_SENT_TEXT
        return _SyntheticResponse(model or "cacheguard", text if isinstance(text, str) else _NOT_SENT_TEXT, api_mode)
    except Exception:
        return None


# Longest wait the platform can express, as in Hermes: about 49.7 days on Windows, one year elsewhere.
_MAX_WAIT_S = 4294966 if sys.platform == "win32" else 31536000


def _approval_timeout() -> int:
    """Seconds the box waits: approvals.timeout from the Hermes config (default 300), capped like Hermes."""
    try:
        raw = int((config._raw_config().get("approvals") or {}).get("timeout", 300))
    except Exception:
        return 300
    return min(raw, _MAX_WAIT_S)


def _denied(deny_text: Any, outcome: str) -> str:
    """The deny text; a callable builds it once we know if the user cancelled or timed out."""
    return deny_text(outcome, _approval_timeout()) if callable(deny_text) else deny_text


def _deny_text(session_id: str | None = None, gap_s: float | None = None,
               outcome: str = "cancelled", timeout: int | None = None, cause: str | None = None) -> str:
    text = _NOT_SENT_TEXT
    if config._finite_number(gap_s):
        text += f" after a pause of {_pause_text(gap_s)}"
    if outcome == "timeout" and timeout is not None:
        text += (" and " if config._finite_number(gap_s) else " after ") + f"{timeout}s timeout"
    elif outcome == "undelivered":
        text += (" and " if config._finite_number(gap_s) else " · ") + "the prompt could not be delivered"
    elif config._finite_number(gap_s):
        text += " (cancelled)"
    elif cause:
        text += f" ({cause})"
    text += "\nSend it again, or start /new and paste it."
    if session_id:
        text += " Session ID: " + json.dumps(str(session_id), ensure_ascii=False)[1:-1]  # one line, whatever the id
    return text


# --- minimal synthetic chat-completions response (transport contract) -------- #
class _SyntheticMessage:
    def __init__(self, content: str) -> None:
        self.role = "assistant"
        self.content = content
        self.tool_calls = None


class _SyntheticChoice:
    def __init__(self, message: _SyntheticMessage) -> None:
        self.index = 0
        self.finish_reason = "stop"
        self.message = message


# api_modes whose transport normaliser accepts the fake object. Anything else
# (e.g. a future wire) fails open: warn line only, never block.
_CHAT_MODES = ("chat_completions", "bedrock_converse")  # bedrock normaliser accepts a .choices namespace
_SYNTHETIC_API_MODES = _CHAT_MODES + ("anthropic_messages", "codex_responses")


def _api_mode_supported(kwargs: dict[str, Any]) -> bool:
    mode = kwargs.get("api_mode")
    return not mode or mode in _SYNTHETIC_API_MODES  # unset == legacy chat_completions


class _SyntheticResponse:
    """Fake provider reply shaped for the active api_mode's transport."""

    def __init__(self, model: str, content: str, api_mode: Any = None) -> None:
        self.model = model
        self.id = "cacheguard"
        self.usage = None
        if api_mode == "anthropic_messages":
            self.type = "message"
            self.role = "assistant"
            self.content = [SimpleNamespace(type="text", text=content)]
            self.stop_reason = "end_turn"
            self.model_extra = {}
        elif api_mode == "codex_responses":
            self.status = "completed"
            self.incomplete_details = None
            self.error = None
            self.output_text = content
            self.output = [SimpleNamespace(
                type="message", role="assistant", status="completed", id="cacheguard-msg",
                content=[SimpleNamespace(type="output_text", text=content, annotations=[])],
            )]
        else:
            self.choices = [_SyntheticChoice(_SyntheticMessage(content))]
