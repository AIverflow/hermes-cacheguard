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
_HELD: dict[str, str] = {}  # session_id -> sha256 of the user message the pre_llm_call hook just blocked
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
    cfg = dict(config.DEFAULTS)
    sid = kwargs.get("session_id")
    if isinstance(sid, str) and not auxiliary and kwargs.get("api_call_count") == 1:
        _start_turn(sid)
    try:
        cfg = config._cfg()
        surface_mode = config._surface_mode(kwargs, cfg)
        triggered = None if auxiliary else _evaluate(kwargs, cfg, surface_mode)
        if triggered:
            _, model, prev, _ = triggered
            if prev is not None and isinstance(sid, str) and sid:
                _CHANGED[sid] = (prev, model)
            _act(kwargs, surface_mode, f"model changed {prev} → {model}" if prev is not None else "")
            acted = True
    except Exception:
        pass  # the plugin failed: the message goes out
    return _send(kwargs, next_call, acted, bool(cfg.get("display_on_success")))


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
        verdict = measure._record_response(kwargs, response, dispatch_ts, response_ts, acted)
        if verdict is not None:
            _queue_outcome(kwargs, verdict, display_on_success)
    except Exception:
        pass
    return response


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
            return f"cache lost, {measure._short_tokens(reloaded)} rebilled, model changed"
        return f"{measure._short_tokens(row['cache_read_tokens'])} reused, model changed"
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
        return f"cache lost, {measure._short_tokens(row['miss_delta'])} rebilled{pause}"
    return f"{measure._short_tokens(row['cache_read_tokens'])} reused{pause}"


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


_WITHHELD_PART_TYPES = ("text", "input_text")


def _drop_withheld(text: str) -> str:
    """The text without the paragraphs that end with Hermes's withheld line (a workspace tag may sit above it)."""
    if config._WITHHELD not in text:
        return text
    return "\n\n".join(p for p in text.split("\n\n") if p.split("\n")[-1] != config._WITHHELD)


def _without_withheld(item: Any) -> Any:
    """A typed user row without the withheld line Hermes merged into it; the same object when there is none.

    The web UI's /retry sends that line in place of the blocked text. A row that is only that line
    stays as it is (dropping it could break the order of roles); the BLOCKED reply after it removes it.
    """
    if not _is_plain_user(item):
        return item
    content = item.get("content")
    if isinstance(content, str):
        new = _drop_withheld(content)
        return item if new == content or not new else {**item, "content": new}
    if not isinstance(content, list):
        return item
    parts: list[Any] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") in _WITHHELD_PART_TYPES and isinstance(part.get("text"), str):
            new = _drop_withheld(part["text"])
            if new != part["text"]:
                if not new:
                    continue  # nothing left in this part
                part = {**part, "text": new}
        parts.append(part)
    if len(parts) == len(content) and all(a is b for a, b in zip(parts, content, strict=True)):
        return item
    if not any(isinstance(p, dict) and p.get("type") in _WITHHELD_PART_TYPES and p.get("text") for p in parts):
        return item  # only the withheld line was there
    return {**item, "content": parts}


def _clean_history(items: list[Any]) -> list[Any]:
    """The history without our BLOCKED replies: each one is dropped with the question it answered
    (the web UI saves two rows for it: the typed text, then Hermes's withheld placeholder).
    Withheld lines merged into a row that stays (after a web UI /retry) are cut out of it."""
    out: list[Any] = []
    for item in items:
        if _is_blocked_reply(item):
            while out and _is_plain_user(out[-1]):
                out.pop()  # the question was never sent to the model
            continue
        out.append(_without_withheld(item))
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
    """Keep cacheguard's own lines (and the withheld lines of blocked messages) in the chat but out of what
    the model receives. Never raises."""
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


def _act(kwargs: dict[str, Any], mode: Any, warn_note: str) -> None:
    """Apply a mode ("off" | "warn" | "block"): mark the session for the result line.

    Nothing is printed before the send: warn_note ("" or a short reason) marks the session, and the
    result line after the reply starts with `WARN` or `OK`. Block is done by the pre_llm_call hook
    (_on_pre_llm_call); here it only warns.
    """
    if mode == "off":
        return
    if isinstance(kwargs.get("session_id"), str) and kwargs.get("session_id"):
        _WARNED[kwargs["session_id"]] = warn_note


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
    if surface_mode not in ("warn", "block"):  # block only warns here: the pre_llm_call hook does the blocking
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


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _on_pre_llm_call(**kwargs: Any) -> Any:
    """Block a large message after a pause (Hermes pre_llm_call hook). Never raises: any error lets it go.

    A blocked message is not sent. The user sends it with /retry or by sending the same text again;
    that one resend is let through without a new check.
    """
    try:
        # Without the middleware the chat's time never moves on: every message would be blocked.
        if config._is_unattended(kwargs) or _is_auxiliary(kwargs) or not health._healthy("middleware"):
            return None
        sid = kwargs.get("session_id")
        text = kwargs.get("user_message")
        if isinstance(text, list):  # the TUI passes a message with an image as its content parts
            text = "\n\n".join(p["text"] for p in text if _is_text_part(p))
        held = _HELD.pop(sid, None) if isinstance(sid, str) else None
        # The web UI's /retry sends Hermes's withheld line in place of the blocked text (kept in the row above).
        if held is not None and isinstance(text, str) and (held == _text_hash(text) or text == config._WITHHELD):
            return None  # the resend of the message just blocked
        cfg = config._cfg()
        if config._surface_mode(kwargs, cfg) != "block" or confirm._single_query():
            return None
        history = kwargs.get("conversation_history")
        if not isinstance(sid, str) or not sid or not isinstance(text, str):
            return None
        if not isinstance(history, list) or not history or not isinstance(history[-1], dict):
            return None
        content = history[-1].get("content")
        image = _message_text(history[-1]) is None
        if image and (not isinstance(content, list) or not cfg["block_images"]):
            return None  # an image or other media: /retry refuses it, so only a warning (unless block_images)
        rec = storage._get_session(sid)
        stored = rec.get("model")
        payload = {
            "session_id": sid, "platform": kwargs.get("platform"), "api_call_count": 1,
            "provider": stored.split("/", 1)[0] if isinstance(stored, str) else None,
            "model": kwargs.get("model"), "api_mode": rec.get("api_mode"),
            "request": {"messages": history},
        }
        triggered = _evaluate(payload, cfg, "block")
        if not triggered:
            return None
        size, model, prev, gap_s = triggered
        message = confirm._block_text(size, model, prev, gap_s, sid, image, tiny=cfg["tiny_block"])
        config._put_capped(_HELD, sid, _text_hash(text))
        return {"action": "block", "message": message}
    except Exception:
        return None
