"""The texts of a block: the pause, the past cold counts, and the reply shown when a message is blocked."""

from __future__ import annotations

import json
import os
import time

from . import config, measure, storage


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


def _clock() -> str:
    """The time shown in the BLOCKED text and the result line, on the clock of the machine running Hermes."""
    return time.strftime("%H:%M")


def _block_text(size: int, model: str, prev: str | None, gap_s: float | None,
                session_id: str | None = None, image: bool = False, tiny: bool = False) -> str:
    """The reply shown when the pre_llm_call hook blocks a message (a model change or a pause).

    image: the message has an image, which Hermes' /retry refuses: it must be attached again.
    tiny: one short line, with no session id and no /retry."""
    cold = None if prev is not None else _cold_counts(model, gap_s)
    if tiny:
        tokens = f"~{measure._short_tokens(size)}t"
        if prev is not None:
            head = f"{prev} → {model} · {tokens}"
        else:
            head = f"{measure._short_pause(gap_s)} 🕐 · {tokens}" + (f" · {cold[0]}/{cold[1]}" if cold is not None else "")
        return config._BLOCKED_PREFIX + f"{_clock()} · " + head + (" · attach the image again" if image else "")
    if prev is not None:
        head = f"model changed {prev} → {model} · ~{size:,} tokens may be rebilled"
    else:
        head = (f"{_pause_text(gap_s)} pause 🕐 · ~{size:,} tokens may be rebilled"
                + (f" · lost {cold[0]} of {cold[1]} similar pauses" if cold is not None else ""))
    how = "attach the image again and send it again" if image else "/retry, or send it again"
    last = "Or start /new and paste it."
    if session_id:
        last += " Session ID: " + json.dumps(str(session_id), ensure_ascii=False)[1:-1]
    return config._BLOCKED_PREFIX + f"{_clock()} · " + head + f"\nTo send it anyway: {how}.\n" + last
