"""What the journal says: risk of a send, one observation per call, cold-cache verdicts and the pause table."""

from __future__ import annotations

import json
from typing import Any

from . import config, guard, storage

_IMAGE_TOKENS = 1500  # flat estimate per image; its base64 text is not what the provider bills


def _strip_images(part: Any) -> tuple[Any, int]:
    """Copy of `part` with image payload strings emptied, and how many images were found."""
    if isinstance(part, list):
        found = 0
        out = []
        for item in part:
            item, n = _strip_images(item)
            out.append(item)
            found += n
        return out, found
    if not isinstance(part, dict):
        return part, 0
    found = 0
    out = {}
    for key, value in part.items():
        if key == "image_url" and isinstance(value, str):
            out[key], found = "", found + 1
        elif key == "image_url" and isinstance(value, dict) and isinstance(value.get("url"), str):
            out[key], found = {**value, "url": ""}, found + 1
        elif key == "source" and isinstance(value, dict) and isinstance(value.get("data"), str):
            out[key], found = {**value, "data": ""}, found + 1
        else:
            out[key], n = _strip_images(value)
            found += n
    return out, found


def score_risk(session: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Score current context size and idle gap without reading state or the clock."""
    settings = payload.get("settings") or {}  # from config._cfg(): size_k and pause_m are valid
    floor = config._size_tokens(settings.get("size_k", config.DEFAULTS["size_k"]))
    window = config._pause_seconds(settings.get("pause_m", config.DEFAULTS["pause_m"]))

    request = payload.get("request")
    size = 0
    if isinstance(request, dict):
        messages = request.get("messages") or request.get("input") or []
        tools = request.get("tools") or []
        pieces = [messages, tools]
        pieces.extend(value for value in (request.get("system"), request.get("instructions")) if value)
        try:
            chars = images = 0
            for part in pieces:
                part, n = _strip_images(part)
                chars += len(json.dumps(part, ensure_ascii=False, default=str))
                images += n
            size = chars // 4 + images * _IMAGE_TOKENS
        except Exception:
            size = 0
    historical = session.get("prompt_tokens")
    if config._finite_number(historical) and historical >= 0:
        size = max(size, int(historical))
    tokens_at_risk = max(0, int(size))

    now = payload.get("now")
    last = session.get("last_api_ts")
    # A stored time in the future (clock went back) tells nothing: pause unknown.
    if not (config._finite_number(last) and last >= 0) or (config._finite_number(now) and last > now):
        last = None
    touched = session.get("cache_touch_ts")
    if config._finite_number(touched) and config._finite_number(last) and touched > last and not (
            config._finite_number(now) and touched > now):
        last = touched
    gap = None
    if config._finite_number(now) and now >= 0 and config._finite_number(last) and last >= 0:
        gap = max(0.0, float(now) - float(last))
    return {
        "tokens_at_risk": tokens_at_risk,
        "factors": {
            "gap_s": gap,
            "window_crossed": gap is not None and gap >= window,
            "floor_crossed": tokens_at_risk >= floor,
        },
    }


_TRIM_TOLERANCE = 0.05  # a prompt this much smaller (or less) than the last is a trim, not a rewrite


def detect_observation(state: dict[str, Any], attempt: dict[str, Any]) -> dict[str, Any]:
    """Build one immutable, domain-aware observation from a successful attempt."""
    session = state.get("session") or {}
    model = attempt.get("model") or "unknown/unknown"
    api_mode = attempt.get("api_mode")
    last_api_ts = session.get("last_api_ts") if isinstance(session, dict) else None
    exact_session = (isinstance(session, dict) and isinstance(session.get("api_mode"), str)
                     and bool(session.get("api_mode")) and config._finite_number(last_api_ts) and last_api_ts >= 0)
    previous = None  # the session file holds the last prompt of the model and API mode now in use
    if exact_session and session.get("model") == model and session.get("api_mode") == api_mode:
        last_prompt = session.get("prompt_tokens")
        if config._finite_number(last_prompt) and last_prompt >= 0:
            previous = last_prompt

    prompt = attempt.get("prompt_tokens")
    read = attempt.get("cache_read_tokens")
    write = attempt.get("cache_write_tokens")
    usage_missing = prompt is None and read is None and write is None
    if usage_missing:
        cause = "usage_missing"
    elif exact_session and (session.get("model") != model or session.get("api_mode") != api_mode):
        cause = "model_switch"
    elif not exact_session or previous is None:
        cause = "start"
    elif attempt.get("prefix_changed") is True or prompt is not None and prompt < previous and previous - prompt > _TRIM_TOLERANCE * previous:  # the start of the chat is not what the last call sent
        cause = "rewrite"
    else:
        cause = None

    gap = None
    dispatch_ts = attempt.get("dispatch_ts")
    if exact_session and config._finite_number(dispatch_ts):  # exact_session: last_api_ts is finite
        gap = max(0.0, float(dispatch_ts) - float(last_api_ts))
    if not exact_session:
        previous = None
    return {
        "ts": attempt.get("ts"), "model": model, "api_mode": api_mode, "gap_s": gap,
        "prompt_tokens": prompt, "cache_read_tokens": read, "cache_write_tokens": write, "prev_prompt_tokens": previous, "cause": cause,
    }


_WARMUP_HITS = 2  # cache reads a model must have shown before a call that read 0 is judged
_COLD_SHARE = 0.30  # a call that reloads at least this share of the prompt lost the cache


def _cache_lost(reloaded: int, baseline: int) -> bool:
    """The one "cache lost" rule, for every turn."""
    return reloaded >= _COLD_SHARE * baseline


def _reloaded_on_change(row: dict[str, Any]) -> int | None:
    """Tokens a model-change turn had to reload ("up to": the new message was never cached),
    or None when the call reported no usage."""
    prompt, read = row.get("prompt_tokens"), row.get("cache_read_tokens")
    if config._finite_number(prompt) and config._finite_number(read) and prompt > 0:
        return max(0, int(prompt) - int(read))
    return None


def evaluate_observations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return read-time verdicts, preserving source rows and append order."""
    result = []
    positive_reads = {}
    for source in rows:
        row = dict(source)
        prompt, read, expected = (source.get("prompt_tokens"), source.get("cache_read_tokens"),
                                  source.get("prev_prompt_tokens"))
        model, mode = source.get("model"), source.get("api_mode")
        known_domain = (isinstance(model, str) and "/" in model
                        and model.split("/", 1)[0] not in ("", "unknown")
                        and model.split("/", 1)[1] not in ("", "unknown")
                        and isinstance(mode, str) and mode not in ("", "unknown"))
        evaluable = bool(known_domain and source.get("cause") is None
                         and isinstance(expected, int) and not isinstance(expected, bool) and expected > 0
                         and isinstance(prompt, int) and not isinstance(prompt, bool) and prompt >= 0
                         and isinstance(read, int) and not isinstance(read, bool) and read >= 0)
        comparable = evaluable
        needs_hits = 0  # cache reads still missing before this call (which read 0) can be judged
        if evaluable and read == 0:
            needs_hits = max(0, _WARMUP_HITS - positive_reads.get((model, mode), 0))
            evaluable = not needs_hits
        delta = max(0, expected - read) if evaluable else None
        cold = _cache_lost(delta, expected) if evaluable else None
        if evaluable:  # never report more rebilled than was sent
            delta = max(0, min(delta, min(expected, prompt) - read))
        row.update(comparable=comparable, evaluable=evaluable, needs_hits=needs_hits, miss_delta=delta, cold=cold)
        result.append(row)
        if known_domain:
            value = source.get("cache_read_tokens")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                domain = (source.get("model"), source.get("api_mode"))
                positive_reads[domain] = positive_reads.get(domain, 0) + 1
    return result


def _record_response(payload: dict[str, Any], response: Any, dispatch_ts: float, response_ts: float,
                     acted: bool) -> dict[str, Any] | None:
    """Keep the call's row and session update. The verdict (it needs the whole journal) only when
    `acted`: the guard warned, so a result line is due."""
    raw_usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    prompt = cached = written = None
    if raw_usage is not None:
        from agent.usage_pricing import normalize_usage

        usage = normalize_usage(raw_usage, provider=payload.get("provider"), api_mode=payload.get("api_mode"))
        prompt, cached, written = usage.prompt_tokens, usage.cache_read_tokens, usage.cache_write_tokens
        if any(not isinstance(x, int) or isinstance(x, bool) or x < 0 for x in (prompt, cached, written)):
            raise ValueError("invalid normalized usage")
    sid = str(payload.get("session_id") or "")
    attempt = {
        "ts": response_ts, "dispatch_ts": dispatch_ts,
        "model": config._model_key(payload.get("provider"), payload.get("model")),
        "api_mode": str(payload.get("api_mode") or "unknown"),
        "prompt_tokens": prompt, "cache_read_tokens": cached, "cache_write_tokens": written,
    }
    auxiliary = guard._is_auxiliary(payload)
    prefix = None if auxiliary else guard._take_prefix(sid, str(payload.get("api_request_id") or ""))
    if prefix is not None:
        attempt["prefix_changed"] = prefix[2]
    row = None
    with storage._storage_lock():
        rows = storage._read_observations() if acted else []
        rec = storage._read_session(sid)
        if not auxiliary:
            row = detect_observation({"session": rec}, attempt)
        if row and config._finite_number(row["gap_s"]) and row["gap_s"] >= config._MIN_PAUSE_S:
            storage._write_observation(row)  # only what the table and the block text can use
            storage._maybe_trim_observations(config._cfg()["journal_rows"])
        if not auxiliary:
            updated = dict(rec, last_api_ts=response_ts, model=attempt["model"], api_mode=attempt["api_mode"])
            if prompt is not None:
                updated["prompt_tokens"] = prompt
            if prefix is not None:
                updated["prefix_n"], updated["prefix_hash"] = prefix[0], prefix[1]
            storage._write_json_atomic(storage._session_file(sid), updated)
        elif (attempt["model"] == rec.get("model") and attempt["api_mode"] == rec.get("api_mode")
              and config._finite_number(rec.get("prompt_tokens")) and rec["prompt_tokens"] > 0
              and cached is not None and cached >= 0.70 * rec["prompt_tokens"]):
            # A background call that read most of the chat kept the cache warm: restart
            # the guard's idle clock only. Stats keep measuring pauses between main calls.
            storage._write_json_atomic(storage._session_file(sid), dict(rec, cache_touch_ts=response_ts))
    # A display failure must not lose the row or replay the provider call.
    return evaluate_observations(rows + [row])[-1] if acted and row else None


# Edges sit 1 minute past 5 min, 20-min steps and 1 h: the measured pause runs a
# bit longer than the time the provider counts.
_GAP_EDGES = [360.0, 1260.0, 2460.0, 3660.0, 5460.0, 7260.0]
_GAP_LABELS = ["1-6m", "6-21m", "21-41m", "41-61m", "61-91m", "91-121m", ">2h01"]
_MIN_CHANCE_CALLS = 10  # measurements in a pause bucket before the block text shows lost/calls
_MIN_TABLE_CALLS = 5  # measurements before the summary shows the cold-cache table
_MIN_SUGGEST_CALLS = 20  # measurements of a model before the table suggests a pause for it
_SUGGEST_LOSS = 0.10  # suggested pause: the longest one where at most this share of calls lost the cache
_SUGGEST_SUPPORT = 5  # measurements at or past a pause before it can be suggested


def _measured_gap(row: dict[str, Any]) -> float | None:
    """The pause of a call the table counts, None for a call it leaves out."""
    gap = row.get("gap_s")
    if not config._finite_number(gap) or gap < config._MIN_PAUSE_S or not row["evaluable"]:
        return None
    return float(gap)


def _cold_cells(evaluated: list[dict[str, Any]],
                model_filter: str = "") -> dict[str, list[dict[str, int]]]:
    """Calls and cold calls per model and pause bucket."""
    models: dict[str, list[dict[str, int]]] = {}
    for row in evaluated:
        model = str(row.get("model") or "unknown")
        if model_filter and model_filter not in model.lower():
            continue
        cells = models.setdefault(model, [{"calls": 0, "cold": 0} for _ in _GAP_LABELS])
        gap = _measured_gap(row)
        if gap is None:
            continue
        cell = cells[config._bucket_index(gap, _GAP_EDGES)]
        cell["calls"] += 1
        cell["cold"] += int(bool(row["cold"]))
    return models


def _loss_blocks(points: list[tuple[float, bool]]) -> list[tuple[float, float, float, int]]:
    """Isotonic fit (pool adjacent violators) of 'lost' on the pause: the loss share never falls
    as the pause grows. Blocks of (first pause, last pause, loss share, calls)."""
    by_gap: dict[float, list[int]] = {}
    for gap, lost in points:
        count = by_gap.setdefault(gap, [0, 0])
        count[0] += int(lost)
        count[1] += 1
    blocks: list[list[Any]] = []  # [first, last, lost, calls]
    for gap in sorted(by_gap):
        lost, calls = by_gap[gap]
        blocks.append([gap, gap, lost, calls])
        while len(blocks) > 1 and blocks[-2][2] * blocks[-1][3] > blocks[-1][2] * blocks[-2][3]:
            top = blocks.pop()
            blocks[-1][1], blocks[-1][2], blocks[-1][3] = top[1], blocks[-1][2] + top[2], blocks[-1][3] + top[3]
    return [(b[0], b[1], b[2] / b[3], b[3]) for b in blocks]


def _short_pause(seconds: float) -> str:
    """5m, 16m, 1h, 1h20 (whole minutes, rounded down)."""
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"{minutes}m"
    return f"{hours}h{minutes:02d}" if minutes else f"{hours}h"


def _short_tokens(count: int) -> str:
    """23k, 999, 1.2M (nearest thousand; exact under 1,000)."""
    count = max(0, int(count))
    if count < 1000:
        return str(count)
    thousands = (count + 500) // 1000
    if thousands < 1000:
        return f"{thousands}k"
    return f"{count / 1_000_000:.1f}M"


def _suggested_pause(points: list[tuple[float, bool]]) -> str:
    """The longest pause whose fitted loss share is _SUGGEST_LOSS or less; none when even the
    shortest is above (a + after the pause: no block above); never past the pause that
    _SUGGEST_SUPPORT measurements reach; — below _MIN_SUGGEST_CALLS measurements."""
    if len(points) < _MIN_SUGGEST_CALLS:
        return "—"
    cap = sorted(gap for gap, _ in points)[-_SUGGEST_SUPPORT]  # one long lucky pause is not enough
    best = None
    for _, last, share, _ in _loss_blocks(points):
        if share > _SUGGEST_LOSS:
            return "none" if best is None else _short_pause(best)
        best = min(last, cap)
    return _short_pause(best) + "+"  # no block above the limit: nothing lost beyond it


def _suggested_pauses(evaluated: list[dict[str, Any]], model_filter: str = "") -> dict[str, str]:
    """The `suggested` column per model. A model that fails to compute gets —."""
    points: dict[str, list[tuple[float, bool]]] = {}
    for row in evaluated:
        try:
            model = str(row.get("model") or "unknown")
            gap = _measured_gap(row)
            if gap is not None and not (model_filter and model_filter not in model.lower()):
                points.setdefault(model, []).append((gap, bool(row["cold"])))
        except Exception:
            continue
    out = {}
    for model, model_points in points.items():
        try:
            out[model] = _suggested_pause(model_points)
        except Exception:
            out[model] = "—"
    return out
