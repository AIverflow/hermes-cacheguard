"""Chat tracking files and the observations journal (what was measured), with the locking around them."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

from . import config, health

_sessions_housekeeping_done = False


def _sessions_dir() -> Path:
    d = config._state_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(session_id: str) -> str:
    """Session id -> filename-safe stem (no path separators, no leading dot)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id))[:150].lstrip(".") or "_"


def _session_file(session_id: str) -> Path:
    return _sessions_dir() / f"{_safe_name(session_id)}.json"


_LOCK_WAIT_S = 0.5


@contextmanager
def _storage_lock():
    """One advisory lock per operation; each caller owns its descriptor.
    Waits at most _LOCK_WAIT_S, then raises (callers fail open: the message is still sent)."""
    with open(config._state_dir() / ".lock", "a+") as lock:
        deadline = time.monotonic() + _LOCK_WAIT_S
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:  # Windows: lock byte 0
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("cacheguard storage is busy") from None
                time.sleep(0.01)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            else:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _replace_file(path: Path, write: Any, binary: bool = False) -> None:
    """Replace `path` in one step: `write(fh)` fills a temp file next to it, which then takes its place."""
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb" if binary else "w", **({} if binary else {"encoding": "utf-8"})) as fh:
            write(fh)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _write_json_atomic(path: Path, data: Any) -> None:
    _replace_file(path, lambda fh: json.dump(data, fh))


def _sessions_housekeeping() -> None:
    """Once per process: keep the newest `chat_files` session files, delete the older ones."""
    global _sessions_housekeeping_done
    if _sessions_housekeeping_done:
        return
    _sessions_housekeeping_done = True
    try:
        limit = config._cfg()["chat_files"]
        files = []
        for f in _sessions_dir().glob("*.json"):
            try:
                files.append((f.stat().st_mtime, f))
            except Exception:
                continue
        files.sort(key=lambda x: x[0], reverse=True)
        for _, f in files[limit:]:
            try:
                f.unlink()
            except Exception:
                continue
    except Exception:
        pass


def _read_session(session_id: str) -> dict[str, Any]:
    """The session's own record. Missing or broken file: empty, so the guard stays silent."""
    _sessions_housekeeping()
    try:
        with open(_session_file(session_id), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _get_session(session_id: str) -> dict[str, Any]:
    try:
        with _storage_lock():
            return _read_session(session_id)
    except Exception:
        return {}


_obs_cache: tuple | None = None  # ((path, mtime_ns, size), rows)


def _write_observation(row: dict[str, Any]) -> None:
    data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    with open(config._obs_path(), "ab+") as fh:
        # A crash can leave a last line without its newline: end it so this row stays separate.
        size = fh.seek(0, os.SEEK_END)
        if size:
            fh.seek(size - 1)
            if fh.read(1) != b"\n":
                data = b"\n" + data
        fh.write(data)


_OLD_ID_KEYS = ("session_id", "api_request_id", "task_id")  # rows of an older version carry them


def _without_old_ids(line: bytes) -> bytes | None:
    """The row without its old identifiers; None for a row nothing reads (see _old_unused_row):
    without its IDs it could no longer be told apart."""
    try:
        row = json.loads(line)
        if _old_unused_row(row):
            return None
        for key in _OLD_ID_KEYS:
            row.pop(key, None)
        return json.dumps(row, ensure_ascii=False).encode("utf-8")
    except Exception:
        return line


def _trim_observations(limit: int) -> None:
    """Keep the journal bounded: once it is 10% over `limit`, keep only the newest `limit` rows.
    Rows that still carry an old identifier are rewritten without it, once.

    Caller holds _storage_lock. Never raises (fail open).
    """
    global _obs_cache
    try:
        path = config._obs_path()
        with open(path, "rb") as fh:  # bytes: one damaged line must not stop the trim
            lines = [ln for ln in fh.read().split(b"\n") if ln.strip()]
        old_ids = any(f'"{key}"'.encode() in ln for ln in lines for key in _OLD_ID_KEYS)
        if old_ids:
            lines = [ln for ln in map(_without_old_ids, lines) if ln is not None]
        elif len(lines) <= limit * 1.1:
            return
        _replace_file(path, lambda fh: fh.writelines(ln + b"\n" for ln in lines[-limit:]), binary=True)
        _obs_cache = None
    except Exception:
        pass


_TRIM_EVERY = 50  # records between trims: reading the whole journal on each one is slow
_trim_countdown = 0  # records to wait before the next trim; 0 = the first record of this process trims


def _maybe_trim_observations(limit: int) -> None:
    """_trim_observations, but only on the first record of this process and every _TRIM_EVERY after."""
    global _trim_countdown
    if _trim_countdown > 0:
        _trim_countdown -= 1
        return
    _trim_countdown = _TRIM_EVERY - 1
    _trim_observations(limit)


_CAUSES = (None, "start", "rewrite", "model_switch", "usage_missing")
_TOKEN_FIELDS = ("prompt_tokens", "cache_read_tokens", "cache_write_tokens")


def _old_unused_row(row: dict[str, Any]) -> bool:
    """A row an older version wrote that nothing reads: a short pause, a background call (it had its own
    task ID), or no pause at all. They go away with the normal trimming."""
    gap, sid, task = row.get("gap_s"), row.get("session_id"), row.get("task_id")
    return (row.get("cause") == "auxiliary"
            or (isinstance(sid, str) and bool(sid) and isinstance(task, str) and bool(task) and task != sid)
            or ("gap_s" in row and (gap is None or (config._finite_number(gap) and gap < config._MIN_PAUSE_S))))


def _valid_observation(row: dict[str, Any]) -> bool:
    """A row the readers can use: the keys they read, with the basic types they expect."""
    def seconds(value: Any) -> bool:
        return config._finite_number(value) and value >= 0

    def count(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    model, mode, prev = row.get("model"), row.get("api_mode"), row.get("prev_prompt_tokens")
    tokens = [row.get(field) for field in _TOKEN_FIELDS]
    missing = sum(t is None for t in tokens)  # no usage at all, or all three counts
    return (all(key in row for key in ("ts", "gap_s", "prev_prompt_tokens", "cause", *_TOKEN_FIELDS))
            and seconds(row["ts"]) and seconds(row["gap_s"])
            and isinstance(model, str) and "/" in model and isinstance(mode, str) and bool(mode)
            and row["cause"] in _CAUSES and (prev is None or seconds(prev))
            and all(t is None or count(t) for t in tokens)
            and missing == (3 if row["cause"] == "usage_missing" else 0))


def _parse_observation_lines(lines: list[bytes]) -> tuple[list[dict[str, Any]], bool]:
    """Valid rows of raw journal lines, and whether any line was skipped as damaged."""
    rows: list[dict[str, Any]] = []
    invalid_rows = False
    for line in lines:
        try:
            row = json.loads(line.decode("utf-8"))
            if isinstance(row, dict) and _old_unused_row(row):
                continue
            valid = isinstance(row, dict) and _valid_observation(row)
        except Exception:
            valid = False
        if valid:
            rows.append(row)
        else:
            invalid_rows = True
    return rows, invalid_rows


def _read_observations() -> list[dict[str, Any]]:
    """The whole journal (damaged lines skipped), parsed again only after it changed."""
    global _obs_cache
    try:
        path = config._obs_path()
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        if _obs_cache and _obs_cache[0] == key:
            return list(_obs_cache[1])
        with open(path, "rb") as fh:
            lines = [ln for ln in fh.read().split(b"\n") if ln.strip()]
        rows, invalid_rows = _parse_observation_lines(lines)
        health._set_data_errors({"invalid observation rows skipped"} if invalid_rows else set())
        _obs_cache = (key, rows)
        return list(rows)
    except FileNotFoundError:
        health._set_data_errors(set())
        return []
    except Exception:
        health._set_data_errors({"observation history unavailable"})
        return []


def _load_observations() -> list[dict[str, Any]]:
    try:
        with _storage_lock():
            return _read_observations()
    except Exception:
        health._set_data_errors({"observation history unavailable"})
        return []
