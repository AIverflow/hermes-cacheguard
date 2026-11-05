"""The middleware: the warning before a send, the result line after the reply, history cleaning."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from . import config, confirm, health, measure, storage

_LOG = logging.getLogger(__name__)
# Per-process memory, mostly by session_id
_PENDING: dict[str, str] = {}  # session_id -> note to post after that session's next reply
_PENDING_TURN: dict[str, str] = {}  # session_id -> turn_id the note belongs to (when known)
_WARNED: dict[str, str] = {}  # session_id -> warn-mode marker, shown with the result
_CHANGED: dict[str, tuple[str, str]] = {}  # session_id -> (previous, new) model of a model-change turn
_NOTICES_SEEN: dict[str, set] = {}  # session_id -> one-off notices already posted to that session
# session_id -> (api_request_id, message count, hash, prefix changed?) of the call being sent;
# saved to the session file once the call is answered (see _record_response)
_PREFIX_PENDING: dict[str, tuple[str, int, str, bool]] = {}


def _is_auxiliary(payload: dict[str, Any]) -> bool:
    """True for detached work: it shares the session ID but has its own task ID (a main task's ID equals it)."""
    sid, task = payload.get("session_id"), payload.get("task_id")
    if not isinstance(sid, str) or not sid:
        return False
    return isinstance(task, str) and bool(task) and task != sid


def _on_llm_execution(**kwargs: Any) -> Any:
    next_call = kwargs["next_call"]
    if config._is_unattended(kwargs):  # cron, kanban, subagents, API: pure pass-through
        return next_call()
    auxiliary = _is_auxiliary(kwargs)
    acted = False
    box: dict[str, Any] = {}  # per call: "shown" once the question went to Hermes, "denied" once a no-send answer is known
    model, deny = "", None  # only for the "not sent" text if the plugin fails after a known Cancel
    cfg = dict(config.DEFAULTS)
    sid = kwargs.get("session_id")
    if isinstance(sid, str) and not auxiliary and kwargs.get("api_call_count") == 1:
        _start_turn(sid)
    try:
        cfg = config._cfg()
        surface_mode = config._surface_mode(kwargs, cfg)
        triggered = None if auxiliary else _evaluate(kwargs, cfg, surface_mode)
        if triggered:
            size, model, prev, gap_s = triggered
            note, command, cause = _box_texts(size, model, prev, gap_s)
            if prev is not None and isinstance(sid, str) and sid:
                _CHANGED[sid] = (prev, model)

            def deny(outcome: str, timeout: int) -> str:
                return confirm._deny_text(kwargs.get("session_id"), gap_s=gap_s, outcome=outcome,
                                          timeout=timeout, cause=cause)

            blocked = _act(kwargs, surface_mode, note, command, deny, model, box)
            if blocked is not None:
                return blocked  # turn ends WITHOUT calling the provider
            acted = True
    except Exception as exc:
        # The plugin failed: the message goes out. Only a Cancel (or timeout) already
        # received stays "not sent"; if even that reply fails, sending beats raising into Hermes.
        if box.get("denied"):
            blocked = confirm._denied_response(model, deny, box["denied"], kwargs.get("api_mode"))
            if blocked is not None:
                return blocked
        elif box.get("shown"):
            confirm._box_broke(exc)  # the box call (or code right after it) failed: no box until restart
    return _send(kwargs, next_call, acted, bool(cfg.get("display_on_success")))


def _box_texts(size: int, model: str, prev: str | None, gap_s: float | None) -> tuple[str, str, str | None]:
    """(warn note, box text, cause shown when the reply is "not sent") for a model change or a pause."""
    if prev is not None:
        return (f"model changed {prev} → {model}",
                f"{config._PREFIX} Model changed\n\n{prev} → {model} · ~{size:,} tokens may be rebilled",
                "model changed")
    return "", confirm._cache_cost_text(size, gap_s, confirm._cold_counts(model, gap_s)), None


def _start_turn(sid: str) -> None:
    """A new turn starts: drop what an earlier turn of this session left behind."""
    _PENDING.pop(sid, None)  # a line left by a turn that had no reply text
    _PENDING_TURN.pop(sid, None)
    _WARNED.pop(sid, None)  # and a warning left by a turn whose provider call failed
    _CHANGED.pop(sid, None)


def _send(kwargs: dict[str, Any], next_call: Any, acted: bool, display_on_success: bool) -> Any:
    """Make the provider call and keep its result line. The call stays outside plugin exception
    recovery: never send twice."""
    if acted:
        try:
            _remember_send(kwargs, time.time())
        except Exception:
            pass
    dispatch_ts = time.time()
    response = next_call()
    response_ts = time.time()
    try:
        if not isinstance(response, confirm._SyntheticResponse):
            verdict = measure._record_response(kwargs, response, dispatch_ts, response_ts, acted)
            if verdict is not None:
                _queue_outcome(kwargs, verdict, display_on_success)
    except Exception:
        pass
    return response


def _short_tokens(count: int) -> str:
    """23k, 999, 1.2M (nearest thousand; exact under 1,000)."""
    count = max(0, int(count))
    if count < 1000:
        return str(count)
    thousands = (count + 500) // 1000
    if thousands < 1000:
        return f"{thousands}k"
    return f"{count / 1_000_000:.1f}M"


_COMPACT_UNITS = {"second": "s", "minute": "min", "hour": "h", "day": "d"}


def _compact_pause(gap_s: float) -> str:
    """45 s, 1 min, 2 h, 3 d (same steps as _pause_text)."""
    count, unit = config._pause_parts(gap_s)
    return f"{count} {_COMPACT_UNITS[unit]}"


def _outcome_text(row: dict[str, Any], change: tuple[str, str] | None = None) -> str:
    """This held attempt's measurement as one short line (the TUI status bar cuts at ~45 characters)."""
    gap = row.get("gap_s")
    pause = f", {_compact_pause(gap)}" if config._finite_number(gap) and change is None else ""
    if change is not None:
        reloaded = measure._reloaded_on_change(row)
        if reloaded is None:
            return "cache unknown, model changed"
        if measure._cache_lost(reloaded, row["prompt_tokens"]):
            return f"cache lost, {_short_tokens(reloaded)} rebilled, model changed"
        return f"{_short_tokens(row['cache_read_tokens'])} reused, model changed"
    if not row["evaluable"]:
        reason = {
            "usage_missing": "usage unavailable",
            "model_switch": "model changed",
            "rewrite": "context changed",
        }.get(row.get("cause"))
        if row.get("cause") == "start":
            return "cache unknown, first call"
        if reason is None:
            if (not row.get("api_mode") or row.get("api_mode") == "unknown"
                    or not isinstance(row.get("model"), str) or "/" not in row["model"]
                    or any(part in ("", "unknown") for part in row["model"].split("/", 1))):
                reason = "provider unknown"
            elif row.get("needs_hits"):
                reason = f"needs {row['needs_hits']} more cache hit{'' if row['needs_hits'] == 1 else 's'}"
            else:
                reason = "little history"
        return f"cache unknown, {reason}{pause}"
    if row["cold"]:
        return f"cache lost, {_short_tokens(row['miss_delta'])} rebilled{pause}"
    return f"{_short_tokens(row['cache_read_tokens'])} reused{pause}"


def _queue_note(session_id: Any, text: str, turn_id: Any = None) -> None:
    if not isinstance(session_id, str) or not session_id:
        return
    dropped = config._put_capped(_PENDING, session_id, text)
    if dropped is not None:
        _PENDING_TURN.pop(dropped, None)
    if isinstance(turn_id, str) and turn_id:
        _PENDING_TURN[session_id] = turn_id
    else:
        _PENDING_TURN.pop(session_id, None)


def _queue_outcome(kwargs: dict[str, Any], row: dict[str, Any],
                   display_on_success: bool = False) -> None:
    """Keep the result line for transform_llm_output; after a warning it starts with `OK` (cache reused) or `WARN`.

    With display_on_success off, a kept cache (not lost) queues nothing.
    """
    sid = kwargs.get("session_id")
    warned = _WARNED.pop(sid, None) if isinstance(sid, str) else None
    change = _CHANGED.pop(sid, None) if isinstance(sid, str) else None
    # A reused cache after a warning is good news: say OK, keep WARN for a loss or an unknown.
    if change is None:
        reused = row.get("evaluable") and not row.get("cold")
    else:
        reloaded = measure._reloaded_on_change(row)
        reused = reloaded is not None and not measure._cache_lost(reloaded, row["prompt_tokens"])
    if reused and not display_on_success:
        return  # cache kept and nothing else to say; state above is already popped
    tag = "" if warned is None else ("OK " if reused else "WARN ")
    body = _outcome_text(row, change)
    _queue_note(sid, f"{config._POST_PREFIX} {tag}{body}", kwargs.get("turn_id"))


def _post(text: str) -> bool:
    """Post one chat notice, outside the model context. Never raises; False if it was not posted."""
    try:
        handle = config._CTX.post_message(text)
        if getattr(handle, "ok", False):
            return True
        _LOG.debug("cacheguard: notice not posted: %s", getattr(handle, "error", None))
    except Exception as exc:
        _LOG.debug("cacheguard: notice not posted: %s: %s", type(exc).__name__, exc)
    return False


def _post_notices(sid: str) -> None:
    """Post each one-off notice this session has not had yet."""
    seen = _NOTICES_SEEN.get(sid)
    if seen is None:
        seen = set()
        config._put_capped(_NOTICES_SEEN, sid, seen)
    for reason in list(health._H["pending_notices"]):
        if reason not in seen and _post(f"{config._POST_PREFIX} {reason}"):
            seen.add(reason)


def _transform_llm_output(**kwargs: Any) -> Any:
    """Post the pending note as a chat notice (Hermes transform_llm_output). Never raises.

    The reply is never changed (None): one-off notices go first, then this turn's result line.
    Without ctx.post_message, or if a post fails, nothing is shown.
    """
    try:
        if config._is_unattended(kwargs):
            return None
        sid = kwargs.get("session_id")
        turn = kwargs.get("turn_id")
        if isinstance(sid, str) and sid:
            _post_notices(sid)
        owner = _PENDING_TURN.get(sid)
        if owner and isinstance(turn, str) and turn and owner != turn:
            return None  # another turn of this session (/btw, background review): not its note
        note = _PENDING.pop(sid, None)
        _PENDING_TURN.pop(sid, None)
        if note:
            _post(note)
    except Exception:
        pass
    return None


_TEXT_PART_TYPES = ("text", "output_text")


def _is_text_part(p: Any) -> bool:
    return isinstance(p, dict) and p.get("type") in _TEXT_PART_TYPES and isinstance(p.get("text"), str)


def _message_text(msg: dict[str, Any]) -> str | None:
    """The text of a plain-text message (string, or a list of text parts only); None for anything else."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content and all(
            _is_text_part(p) for p in content):
        return "\n\n".join(p["text"] for p in content)
    return None


def _is_blocked_reply(msg: Any) -> bool:
    """An assistant message that is one of our BLOCKED texts (never produced by the model)."""
    if not isinstance(msg, dict) or msg.get("role") != "assistant" or msg.get("tool_calls"):
        return False
    text = _message_text(msg)
    return text is not None and text.startswith(config._BLOCKED_PREFIX)


def _is_plain_user(msg: Any) -> bool:
    """A user message that is typed text, not a tool result."""
    if not isinstance(msg, dict) or msg.get("role") != "user" or msg.get("tool_call_id"):
        return False
    content = msg.get("content")
    if isinstance(content, list):
        return not any(isinstance(p, dict) and ("result" in str(p.get("type")) or "output" in str(p.get("type")))
                       for p in content)
    return True


def _clean_history(items: list[Any]) -> list[Any]:
    """The history without our BLOCKED replies: each one is dropped with the question it answered."""
    out: list[Any] = []
    for item in items:
        if _is_blocked_reply(item):
            if out and _is_plain_user(out[-1]):
                out.pop()  # the question was never sent to the model
            continue
        out.append(item)
    return out


def _plain_for_hash(value: Any) -> Any:
    """A message as the provider's cache sees it: `cache_control` markers move from call to call
    (and turn a string into a one-part list), and so do Bedrock's `cachePoint` blocks, so they
    are left out."""
    if isinstance(value, dict):
        out = {k: _plain_for_hash(v) for k, v in value.items() if k != "cache_control"}
        content = out.get("content")
        if (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
                and content[0].get("type") in _TEXT_PART_TYPES and set(content[0]) <= {"type", "text"}
                and isinstance(content[0].get("text"), str)):
            out["content"] = content[0]["text"]
        return out
    if isinstance(value, (list, tuple)):
        return [_plain_for_hash(v) for v in value
                if not (isinstance(v, dict) and set(v) == {"cachePoint"})]
    return value


def _hash_messages(items: list[Any], n: int) -> tuple[str, str]:
    """(hash of the first n messages, hash of all of them), in one pass. Raises on anything
    that is not plain JSON data: callers treat that as "no hash"."""
    whole = hashlib.sha256()
    head = whole.hexdigest() if n <= 0 else ""
    for i, item in enumerate(items):
        line = json.dumps(_plain_for_hash(item), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        whole.update(line.encode("utf-8", "surrogatepass") + b"\n")
        if i + 1 == n:
            head = whole.copy().hexdigest()
    return head, whole.hexdigest()


def _note_prefix(kwargs: dict[str, Any], clean: list[Any]) -> None:
    """Compare the start of this call's cleaned messages with the last answered call of the same chat."""
    sid = kwargs.get("session_id")
    if not isinstance(sid, str) or not sid:
        return
    if _is_auxiliary(kwargs):
        return  # background calls neither read nor move the chat's hash
    rec = storage._get_session(sid)
    count, old = rec.get("prefix_n"), rec.get("prefix_hash")
    known = isinstance(count, int) and not isinstance(count, bool) and count > 0 and isinstance(old, str)
    head, whole = _hash_messages(clean, count if known else 0)
    changed = bool(known and (len(clean) < count or head != old))
    config._put_capped(_PREFIX_PENDING, sid, (str(kwargs.get("api_request_id") or ""), len(clean), whole, changed))


def _take_prefix(sid: str, api_request_id: str) -> tuple[int, str, bool] | None:
    """The (count, hash, changed) noted for this call, or None. Never raises."""
    try:
        pending = _PREFIX_PENDING.pop(sid, None)
        if pending is not None and pending[0] == api_request_id:
            return pending[1], pending[2], pending[3]
    except Exception:
        pass
    return None


def _on_llm_request(**kwargs: Any) -> Any:
    """Keep cacheguard's own lines in the chat but out of what the model receives. Never raises."""
    try:
        if config._is_unattended(kwargs):
            return None
        request = kwargs.get("request")
        if not isinstance(request, dict):
            return None
        for key in ("messages", "input"):
            items = request.get(key)
            if isinstance(items, list):
                clean = _clean_history(items)
                try:
                    _note_prefix(kwargs, clean)
                except Exception:
                    pass  # no hash: the call is judged as before
                if len(clean) != len(items) or any(a is not b for a, b in zip(clean, items, strict=True)):
                    return {"request": {**request, key: clean}, "source": "cacheguard",
                            "reason": "removed cacheguard lines"}
                return None
    except Exception:
        pass
    return None


def _act(kwargs: dict[str, Any], mode: Any,
         warn_note: str, command: str, deny_text: Any, model: str,
         box: dict[str, Any]) -> Any:
    """Apply a mode ("off" | "warn" | "block"). Return a synthetic response to end the turn, else None (send).

    A warning prints nothing: warn_note ("" or a short reason) marks the session, and the result
    line after the reply starts with `WARN` or `OK`. `box` is the caller's per-call marker.
    """
    if mode == "off":
        return None
    shown = False
    if mode == "block" and health._H.get("box_failed"):
        mode = "warn"  # the box failed earlier in this process: warn until Hermes restarts
    if mode == "block" and confirm._single_query():
        mode = "warn"  # `chat -q`: no screen, a prompt would only wait out the timeout
    if mode == "block" and confirm._gateway_approval() is None:
        mode = "warn"  # degraded: no way to ask here (editor/ACP, or a Hermes without the gateway box)
        health._notice(health._NO_BOX_TEXT)
    if config._platform(kwargs) in config._CLI_PLATFORMS:
        command = command.replace("\n\n", "\n", 1)  # the terminal box needs no blank line under its title
    if mode == "block" and confirm._api_mode_supported(kwargs):
        blocked = confirm._gateway_confirmation(kwargs, command, deny_text, model, box)
        shown = True
        if blocked is not None:
            return blocked
    if not shown and isinstance(kwargs.get("session_id"), str) and kwargs.get("session_id"):
        # No panel was shown (the panel already carries the info): say it with the result line.
        _WARNED[kwargs["session_id"]] = warn_note
    return None


def _domain_key(payload: dict[str, Any]) -> str:
    return json.dumps([config._model_key(payload.get("provider"), payload.get("model")),
                       payload.get("api_mode") or "unknown"])


def _guard_sends(rec: dict[str, Any]) -> dict[str, Any]:
    """The saved send times; a wrong type in the session file counts as empty."""
    sends = rec.get("guard_sends")
    return sends if isinstance(sends, dict) else {}


def _remember_send(payload: dict[str, Any], sent_at: float) -> None:
    sid = str(payload.get("session_id") or "")
    with storage._storage_lock():
        rec = storage._read_session(sid)
        sends = dict(_guard_sends(rec))
        sends[_domain_key(payload)] = sent_at
        storage._write_json_atomic(storage._session_file(sid), dict(rec, guard_sends=sends))


def _cooldown_active(rec: dict[str, Any], payload: dict[str, Any], cfg: dict[str, Any], now: float) -> bool:
    sent_at = _guard_sends(rec).get(_domain_key(payload))
    return config._finite_number(sent_at) and 0 <= now - sent_at < config._pause_seconds(cfg["pause_m"])  # future time: clock went back


def _evaluate(kwargs: dict[str, Any], cfg: dict[str, Any], surface_mode: Any) -> tuple[int, str, str | None, float | None] | None:
    """Return (size, model, previous_model, gap_s) when the guard fires, else None.

    previous_model is set (and gap_s is None) when the session's model or provider changed: a
    different model can never reuse the old cache, whatever the idle gap. Otherwise gap_s is the
    idle gap that crossed the pause window.
    """
    # Only the first call of a turn: the read that pays the cold context.
    if int(kwargs.get("api_call_count") or 0) != 1:
        return None
    if surface_mode not in ("warn", "block"):  # block without an approval path acts like warn (see _act)
        return None

    session_id = kwargs.get("session_id") or ""
    model = config._model_key(kwargs.get("provider"), kwargs.get("model"))

    rec = storage._get_session(session_id)
    now = time.time()
    if _cooldown_active(rec, kwargs, cfg, now):
        return None
    prev = rec.get("model") if isinstance(rec.get("model"), str) else None
    mode_changed = (isinstance(rec.get("api_mode"), str) and kwargs.get("api_mode")
                    and rec["api_mode"] != kwargs["api_mode"])
    if prev and (prev != model or mode_changed):
        if mode_changed:
            prev = f"{prev} ({rec['api_mode']} → {kwargs['api_mode']})"
        size = measure.score_risk(rec, {"request": kwargs.get("request")})["tokens_at_risk"]
        if size < config._size_tokens(cfg["size_k"]):
            return None
        return size, model, prev, None
    risk = measure.score_risk(rec, {
        "request": kwargs.get("request"), "now": now, "settings": cfg,
    })
    factors = risk["factors"]
    if not factors["window_crossed"] or not factors["floor_crossed"]:
        return None
    return risk["tokens_at_risk"], model, None, factors["gap_s"]
