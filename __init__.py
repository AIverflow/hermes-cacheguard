"""cacheguard — warn or ask before sending a large context after a pause.

The first API call of each turn is guarded using context-size and idle-time
thresholds. Cache availability is unknown before the provider call; the box
shows only how often the cache was lost after similar pauses before. In block mode,
deny or timeout ends the turn without calling the provider.

Works in the Hermes CLI and in the gateway. CLI, TUI, desktop app, Web UI and
Telegram: tested. Editor (ACP) and other gateway platforms: should work, not yet tested.
Bot Chat is left alone.

The llm_execution middleware records normalized usage from every response, with
no extra provider calls. /cacheguard shows historical call and cold counts
per provider/model and idle range. Cold is a low-reuse classification, not
proof that a cache expired.

Observations live in <hermes home>/cacheguard/observations.jsonl.
Plugin failures allow the provider call to proceed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
import tempfile
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

DEFAULTS: Dict[str, Any] = {
    "cli": "warn",  # terminal, editor
    "desktop": "warn",  # desktop app, web UI
    "gateway": "off",  # Telegram and other messaging chats
    "display_on_success": False,  # show the result line when the cache was kept (loss and unknown always show)
    "size_k": 20,  # thousands of tokens
    "pause_m": 10,
}
_MAX_OBSERVATIONS = 10000  # journal rows kept; trimmed once 10% over


# SQL: skip our own "message not sent" replies.
_NOT_DENY_SQL = "content NOT LIKE '%BLOCKED · message not sent%'"


# --------------------------------------------------------------------------- #
# config / paths
# --------------------------------------------------------------------------- #
_PREFIX = "[CACHEGUARD]"  # the ONE prefix of every line cacheguard adds to a chat
_POST_PREFIX = "CGUARD"  # prefix of the short posted notices (the TUI status bar cuts them at ~45 characters)


_BOT_CHAT: Dict[Tuple[str, str], bool] = {}  # (home, session_id) -> answer, so SQLite opens once per session
_BOT_CHAT_MAX = 64


# Kept so Bot Chat support can be revived later; today _is_unattended() treats it as unattended.
def _is_bot_chat(session_id: str) -> bool:
    """Match Hermes' Bot Chat contract: exact session title plus managed install.

    Use the active profile's state.db (Hermes scopes get_hermes_home per turn), then
    the installed probe's root-wide roster marker. Neither platform nor profile alone
    identifies a Bot Chat; compression copies the canonical title to its child session.
    """
    if not session_id or not _healthy("state_db"):
        return False
    try:
        import sqlite3
        from hermes_constants import get_hermes_home
        from tools.bot_mode_probe import BOT_CHAT_TITLE, is_bot_mode_managed

        home = Path(get_hermes_home())
        key = (str(home), session_id)
        if key in _BOT_CHAT:
            return _BOT_CHAT[key]
        con = sqlite3.connect(_state_db_uri(home), uri=True, timeout=1.0)
        try:
            row = con.execute("SELECT title FROM sessions WHERE id = ?", (session_id,)).fetchone()
        finally:
            con.close()
        answer = bool(row and row[0] == BOT_CHAT_TITLE and is_bot_mode_managed(home))
        if len(_BOT_CHAT) >= _BOT_CHAT_MAX:
            _BOT_CHAT.pop(next(iter(_BOT_CHAT)))
        _BOT_CHAT[key] = answer
        return answer
    except Exception:
        return False


# The one platform table. Unknown platforms count as messaging (gateway).
_CLI_PLATFORMS = frozenset({"cli", "tui", "local", "acp"})
_DESKTOP_PLATFORMS = frozenset({"desktop", "webui"})
_UNATTENDED_PLATFORMS = frozenset({"cron", "kanban", "subagent", "api_server", "webhook", "msgraph_webhook"})


def _platform(payload: Dict[str, Any]) -> str:
    return str(getattr(payload.get("platform"), "value", payload.get("platform")) or "cli").strip().lower()


def _session_env(name: str) -> str:
    try:
        from gateway.session_context import get_session_env

        return get_session_env(name, "") or ""
    except Exception:
        return os.environ.get(name, "") or ""


def _is_unattended(payload: Dict[str, Any]) -> bool:
    """Nobody is there to answer or read: cacheguard stays out of the way.

    Kanban workers and cron runs can carry `cli`; Hermes marks them with these env vars.
    Bot Chat is left alone too (not stable enough yet); its state.db lookup runs last.
    """
    if _platform(payload) in _UNATTENDED_PLATFORMS or _session_env("HERMES_KANBAN_TASK").strip():
        return True
    if _session_env("HERMES_CRON_SESSION").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return _is_bot_chat(str(payload.get("session_id") or ""))


def _is_cli_platform(payload: Dict[str, Any]) -> bool:
    return _platform(payload) in _CLI_PLATFORMS | _DESKTOP_PLATFORMS


def _surface_mode(payload: Dict[str, Any], cfg: Dict[str, Any]) -> Any:
    if _platform(payload) in _DESKTOP_PLATFORMS:
        return cfg["desktop"]
    return cfg["cli"] if _is_cli_platform(payload) else cfg["gateway"]


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _known_settings(section: Dict[str, Any]) -> Dict[str, Any]:
    """Settings this plugin knows."""
    known = {k: v for k, v in section.items() if k in DEFAULTS}
    for key in ("cli", "desktop", "gateway"):
        if known.get(key) is False:  # YAML 1.1 reads an unquoted `off` as False
            known[key] = "off"
    return known


def _valid_number(name: str, value: Any) -> bool:
    """The one rule for pause_m and size_k, in config.yaml and in `/cacheguard set`."""
    if not _finite_number(value):
        return False
    if name == "pause_m":
        return value >= 1 and math.isfinite(float(value) * 60)
    return 0 <= value <= 2000  # size_k: thousands of tokens


def _pause_seconds(pause_m: Any) -> float:
    return float(pause_m) * 60


def _size_tokens(size_k: Any) -> int:
    return round(float(size_k) * 1000)


def _raw_config() -> Dict[str, Any]:
    """config.yaml as a mapping, for reading only. Newer Hermes has a read-only call that skips
    the deep copy; the plain call is the fallback. Never change what this returns."""
    from hermes_cli import config as hermes_config

    read = getattr(hermes_config, "read_raw_config_readonly", None) or hermes_config.read_raw_config
    return read() or {}


def _cfg() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    errors = set()
    try:
        raw = _raw_config()
        if not isinstance(raw, dict):
            raise TypeError("configuration root is not a mapping")
        # The one place (written by `/cacheguard set`): plugins.entries.cacheguard.settings
        plugins = raw.get("plugins")
        if plugins is not None and not isinstance(plugins, dict):
            errors.add("invalid official cacheguard settings; using defaults")
        entries = plugins.get("entries") if isinstance(plugins, dict) else None
        if entries is not None and not isinstance(entries, dict):
            errors.add("invalid official cacheguard settings; using defaults")
        entry = entries.get("cacheguard") if isinstance(entries, dict) else None
        official = entry.get("settings") if isinstance(entry, dict) else None
        if entry is not None and not isinstance(entry, dict):
            errors.add("invalid official cacheguard settings; using defaults")
        if isinstance(official, dict):
            cfg.update(_known_settings(official))
        elif official is not None:
            errors.add("invalid official cacheguard settings; using defaults")
    except Exception:
        cfg = dict(DEFAULTS)
        errors.add("configuration unavailable; using defaults")
        _set_config_errors(errors)
        return cfg

    def invalid(key: str) -> None:
        cfg[key] = DEFAULTS[key]
        errors.add(f"invalid {key}; using default")

    if not isinstance(cfg["display_on_success"], bool):
        invalid("display_on_success")
    for key in ("cli", "desktop", "gateway"):
        if not isinstance(cfg[key], str) or cfg[key] not in ("off", "warn", "block"):
            invalid(key)
    for key in ("pause_m", "size_k"):
        if not _valid_number(key, cfg[key]):
            invalid(key)
    _set_config_errors(errors)
    return cfg


def _set_config_errors(errors: set) -> None:
    # _H is initialized before config is read by registered plugin callbacks.
    _H["config_errors"] = errors
    for reason in errors:
        _notice(reason)


def _state_db_uri(home: Any) -> str:
    """Read-only SQLite URI for Hermes' state.db, valid on POSIX and Windows."""
    return (Path(home).absolute() / "state.db").as_uri() + "?mode=ro"


def _state_dir() -> Path:
    try:
        from hermes_constants import get_hermes_home

        base = Path(get_hermes_home())
    except Exception:
        base = Path(os.path.expanduser("~/.hermes"))
    d = base / "cacheguard"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _obs_path() -> Path:
    return _state_dir() / "observations.jsonl"


def _bucket_index(gap_s: float, edges: List[float]) -> int:
    for i, edge in enumerate(edges):
        if gap_s < edge:
            return i
    return len(edges)


def _model_key(provider: Optional[str], model: Optional[str]) -> str:
    model = (model or "").strip() or "unknown"
    provider = (provider or "").strip()
    return f"{provider or 'unknown'}/{model}"


# --------------------------------------------------------------------------- #
# storage (tiny, append-only, never raises)
# --------------------------------------------------------------------------- #
_MAX_SESSION_FILES = 200  # chat tracking files kept, newest first
_sessions_housekeeping_done = False


def _sessions_dir() -> Path:
    d = _state_dir() / "sessions"
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
    with open(_state_dir() / ".lock", "a+") as lock:
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
                    raise TimeoutError("cacheguard storage is busy")
                time.sleep(0.01)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            else:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _write_json_atomic(path: Path, data: Any) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _write_private_text(path: Path, text: str) -> None:
    """Write a file readable by the owner only (0600), from the first byte."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)  # also fixes a file that already existed
        except (AttributeError, OSError):
            pass  # Windows: no real file modes
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(text)
    finally:
        if fd >= 0:
            os.close(fd)


def _sessions_housekeeping() -> None:
    """Once per process: keep the newest session files, delete the older ones."""
    global _sessions_housekeeping_done
    if _sessions_housekeeping_done:
        return
    _sessions_housekeeping_done = True
    try:
        files = []
        for f in _sessions_dir().glob("*.json"):
            try:
                files.append((f.stat().st_mtime, f))
            except Exception:
                continue
        files.sort(key=lambda x: x[0], reverse=True)
        for _, f in files[_MAX_SESSION_FILES:]:
            try:
                f.unlink()
            except Exception:
                continue
    except Exception:
        pass


def _read_session(session_id: str) -> Dict[str, Any]:
    """The session's own record. Missing or broken file: empty, so the guard stays silent."""
    _sessions_housekeeping()
    try:
        with open(_session_file(session_id), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _get_session(session_id: str) -> Dict[str, Any]:
    try:
        with _storage_lock():
            return _read_session(session_id)
    except Exception:
        return {}


_obs_cache: Optional[tuple] = None  # ((path, mtime_ns, size), rows)


def _write_observation(row: Dict[str, Any]) -> None:
    data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    with open(_obs_path(), "ab+") as fh:
        # A crash can leave a last line without its newline: end it so this row stays separate.
        size = fh.seek(0, os.SEEK_END)
        if size:
            fh.seek(size - 1)
            if fh.read(1) != b"\n":
                data = b"\n" + data
        fh.write(data)


def _trim_observations(limit: int) -> None:
    """Keep the journal bounded: once it is 10% over `limit`, keep only the newest `limit` rows.

    Caller holds _storage_lock. Never raises (fail open).
    """
    global _obs_cache
    try:
        path = _obs_path()
        with open(path, "rb") as fh:  # bytes: one damaged line must not stop the trim
            lines = [ln for ln in fh.read().split(b"\n") if ln.strip()]
        if len(lines) <= limit * 1.1:
            return
        fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.writelines(ln + b"\n" for ln in lines[-limit:])
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
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


def _parse_observation_lines(lines: List[bytes]) -> Tuple[List[Dict[str, Any]], bool]:
    """Valid rows of raw journal lines, and whether any line was skipped as damaged."""
    rows: List[Dict[str, Any]] = []
    invalid_rows = False
    for line in lines:
        try:
            row = json.loads(line.decode("utf-8"))
        except Exception:
            invalid_rows = True
            continue
        if not isinstance(row, dict):
            invalid_rows = True
            continue
        if ("model" in row and row["model"] is not None and not isinstance(row["model"], str)):
            invalid_rows = True
            continue
        if ("session_id" in row and row["session_id"] is not None
                and not isinstance(row["session_id"], str)):
            invalid_rows = True
            continue
        bad = False
        for field in ("gap_s", "ts"):
            value = row.get(field)
            if field == "ts" and field in row and value is None:
                bad = True
            if value is not None and (
                not _finite_number(value) or value < 0
            ):
                bad = True
        if "task_id" in row and (not isinstance(row["task_id"], str) or not row["task_id"]):
            bad = True
        for field in ("prompt_tokens", "cache_read_tokens", "cache_write_tokens", "prev_prompt_tokens"):
            value = row.get(field)
            if value is not None and (
                not _finite_number(value) or value < 0
            ):
                bad = True
        required = ("ts", "session_id", "model", "api_mode", "api_request_id", "gap_s",
                    "prompt_tokens", "cache_read_tokens", "cache_write_tokens", "prev_prompt_tokens", "cause")
        if (not all(field in row for field in required)
                or not isinstance(row.get("api_mode"), str) or not row.get("api_mode")
                or not isinstance(row.get("model"), str) or "/" not in row.get("model", "")
                or not isinstance(row.get("session_id"), str)
                or not isinstance(row.get("api_request_id"), str)
                or row.get("cause") not in (None, "start", "rewrite", "model_switch", "usage_missing", "auxiliary")):
            bad = True
        tokens = [row.get(f) for f in ("prompt_tokens", "cache_read_tokens", "cache_write_tokens")]
        if any(t is None for t in tokens) != all(t is None for t in tokens):
            bad = True
        if any(t is not None and (not isinstance(t, int) or isinstance(t, bool)) for t in tokens):
            bad = True
        if all(t is None for t in tokens) and row.get("cause") not in ("usage_missing", "auxiliary"):
            bad = True
        if bad:
            invalid_rows = True
            continue
        rows.append(row)
    return rows, invalid_rows


def _read_observations() -> List[Dict[str, Any]]:
    """Read the entire append-only history, re-parsing only after changes."""
    global _obs_cache
    try:
        path = _obs_path()
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        if _obs_cache and _obs_cache[0] == key:
            return list(_obs_cache[1])
        with open(path, "rb") as fh:
            tail = [ln for ln in fh.read().split(b"\n") if ln.strip()]
        rows, invalid_rows = _parse_observation_lines(tail)
        _set_data_errors({"invalid observation rows skipped"} if invalid_rows else set())
        _obs_cache = (key, rows)
        return list(rows)
    except FileNotFoundError:
        _set_data_errors(set())
        return []
    except Exception:
        _set_data_errors({"observation history unavailable"})
        return []


_TAIL_BYTES = 256 * 1024  # how much of the journal's end _record_response reads


def _read_tail_lines() -> Tuple[List[bytes], bool]:
    """Whole non-empty lines in the last _TAIL_BYTES of the journal, and whether older bytes were left out."""
    with open(_obs_path(), "rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        start = max(0, size - _TAIL_BYTES)
        fh.seek(start)
        data = fh.read()
    lines = data.split(b"\n")
    if start:
        lines = lines[1:]  # the first piece may be half a line
    return [ln for ln in lines if ln.strip()], start > 0


_tail_cache: Optional[tuple] = None  # ((path, mtime_ns, size), rows, truncated)


def _read_recent_observations() -> Tuple[List[Dict[str, Any]], bool]:
    """The journal's newest rows (damaged lines skipped), and whether older rows were left out.

    Cheap at any journal size. Does not touch the data-error notice: only the full read judges the file.
    """
    global _tail_cache
    try:
        path = _obs_path()
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        if _tail_cache and _tail_cache[0] == key:
            return list(_tail_cache[1]), _tail_cache[2]
        lines, truncated = _read_tail_lines()
        rows = _parse_observation_lines(lines)[0]
        _tail_cache = (key, rows, truncated)
        return list(rows), truncated
    except FileNotFoundError:
        return [], False
    except Exception:
        _set_data_errors({"observation history unavailable"})
        return [], False


def _load_observations() -> List[Dict[str, Any]]:
    try:
        with _storage_lock():
            return _read_observations()
    except Exception:
        _set_data_errors({"observation history unavailable"})
        return []


# --------------------------------------------------------------------------- #
# observed cache reuse
# --------------------------------------------------------------------------- #
_IMAGE_TOKENS = 1500  # flat estimate per image; its base64 text is not what the provider bills


def _strip_images(part: Any) -> Tuple[Any, int]:
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


def score_risk(state: Dict[str, Any], pre_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Score current context size and idle gap without reading state or the clock."""
    session = state if isinstance(state, dict) else {}
    payload = pre_payload if isinstance(pre_payload, dict) else {}
    settings = payload.get("settings")
    settings = settings if isinstance(settings, dict) else {}

    raw_floor = settings.get("size_k", DEFAULTS["size_k"])
    floor = _size_tokens(raw_floor if _valid_number("size_k", raw_floor) else DEFAULTS["size_k"])
    raw_window = settings.get("pause_m", DEFAULTS["pause_m"])
    window = _pause_seconds(raw_window if _valid_number("pause_m", raw_window) else DEFAULTS["pause_m"])

    request = payload.get("request")
    if not isinstance(request, dict) and any(
        key in payload for key in ("messages", "input", "tools", "system", "instructions")
    ):
        request = payload
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
    if _finite_number(historical) and historical >= 0:
        size = max(size, int(historical))
    tokens_at_risk = max(0, int(size))

    now = payload.get("now", payload.get("now_ts"))
    last = session.get("last_api_ts")
    # A stored time in the future (clock went back) tells nothing: pause unknown.
    if not (_finite_number(last) and last >= 0) or (_finite_number(now) and last > now):
        last = None
    touched = session.get("cache_touch_ts")
    if _finite_number(touched) and _finite_number(last) and touched > last and not (
            _finite_number(now) and touched > now):
        last = touched
    gap = None
    if _finite_number(now) and now >= 0 and _finite_number(last) and last >= 0:
        gap = max(0.0, float(now) - float(last))
    return {
        "tokens_at_risk": tokens_at_risk,
        "factors": {
            "gap_s": gap,
            "window_crossed": gap is not None and gap >= window,
            "floor_crossed": tokens_at_risk >= floor,
        },
    }


# --------------------------------------------------------------------------- #
# middleware: the pre-send warning / block (before the provider call)
# --------------------------------------------------------------------------- #
def _is_auxiliary(payload: Dict[str, Any]) -> bool:
    """Hermes main CLI/gateway task IDs equal the physical session ID.

    Detached work shares cache affinity/session_id but has its own task ID.
    Older journal rows retain that ID in Hermes' structured dispatch ID.
    Do not interpret arbitrary request IDs or classify by task-name allowlists.
    """
    if payload.get("cause") == "auxiliary":
        return True
    sid, task = payload.get("session_id"), payload.get("task_id")
    if not isinstance(sid, str) or not sid:
        return False
    if isinstance(task, str) and task:
        return task != sid
    rid = payload.get("api_request_id")
    prefix = sid + ":"
    if not isinstance(rid, str) or not rid.startswith(prefix):
        return False
    parts = rid[len(prefix):].split(":")
    return bool(len(parts) == 4 and parts[0] and parts[1]
                and parts[2] == "api" and parts[3].isdigit() and parts[0] != sid)


def _is_minted_main_call(payload: Dict[str, Any]) -> bool:
    """`chat -Q` and `-z` (Bot Chat, `-c NAME -Q`) call run_conversation without task_id, so
    Hermes mints a uuid4. That call is the main loop unless it is a fork (background review,
    curator, /btw), which also mints one but runs under a non-foreground write origin.
    """
    sid, task = payload.get("session_id"), payload.get("task_id")
    if not (isinstance(sid, str) and sid and isinstance(task, str) and task and task != sid):
        return False
    try:
        if str(uuid.UUID(task)) != task.lower():
            return False
        rid = payload.get("api_request_id")
        if isinstance(rid, str) and rid and not rid.startswith(f"{sid}:{task}:"):
            return False
        from tools.skill_provenance import get_current_write_origin

        return get_current_write_origin() in ("foreground", "assistant_tool")
    except Exception:
        return False  # unknown: keep it auxiliary


def _on_llm_execution(**kwargs: Any) -> Any:
    next_call = kwargs.get("next_call")
    request = kwargs.get("request")
    if _is_unattended(kwargs):  # cron, kanban, subagents, API: pure pass-through
        return request if next_call is None else next_call()
    if _is_minted_main_call(kwargs):
        kwargs["task_id"] = kwargs["session_id"]  # read and stored as a main-loop call
    auxiliary = _is_auxiliary(kwargs)
    acted = False
    box: Dict[str, Any] = {}  # per call: "shown" once the question went to Hermes, "denied" once a no-send answer is known
    model, deny = "", None  # only for the "not sent" text if the plugin fails after a known Cancel
    cfg = dict(DEFAULTS)
    sid = kwargs.get("session_id")
    if isinstance(sid, str) and not auxiliary and kwargs.get("api_call_count") == 1:
        _PENDING.pop(sid, None)  # a new turn starts: drop a line left by a turn that had no reply text
        _PENDING_TURN.pop(sid, None)
        _WARNED.pop(sid, None)  # and a warning left by a turn whose provider call failed
        _CHANGED.pop(sid, None)
    try:
        if next_call is None:
            return request
        cfg = _cfg()
        surface_mode = _surface_mode(kwargs, cfg)
        is_cli = _is_cli_platform(kwargs)
        changed = None if auxiliary else _evaluate_model_change(kwargs, cfg, surface_mode)
        if changed:
            # A different model/provider can never reuse the old cache, whatever the idle gap.
            size, prev, model = changed
            note = f"model changed {prev} → {model}"
            command = (
                f"{_PREFIX} Model changed\n\n"
                f"{prev} → {model} · ~{size:,} tokens may be rebilled"
            )
            deny = lambda outcome, timeout: _deny_text(  # noqa: E731
                kwargs.get("session_id"), gateway=not is_cli, outcome=outcome, timeout=timeout,
                cause="model changed")
            if isinstance(sid, str) and sid:
                _CHANGED[sid] = (prev, model)
            blocked = _act(kwargs, is_cli, surface_mode, note, command, deny, model, box)
            if blocked is not None:
                return blocked
            acted = True
        else:
            triggered = None if auxiliary else _evaluate(kwargs, cfg, surface_mode)
            if triggered:
                size, model, gap_s = triggered
                estimate = _cold_counts(model, gap_s)
                command = _cache_cost_text(size, gap_s, estimate)
                deny = lambda outcome, timeout: _deny_text(  # noqa: E731
                    kwargs.get("session_id"), gateway=not is_cli, gap_s=gap_s, outcome=outcome,
                    timeout=timeout)
                blocked = _act(kwargs, is_cli, surface_mode, "", command, deny, model, box)
                if blocked is not None:
                    return blocked  # turn ends WITHOUT calling the provider
                acted = True
    except Exception as exc:
        # The plugin failed: the message goes out. Only a Cancel (or timeout) already
        # received stays "not sent"; if even that reply fails, sending beats raising into Hermes.
        if box.get("denied"):
            blocked = _denied_response(model, deny, box["denied"], kwargs.get("api_mode"))
            if blocked is not None:
                return blocked
        elif box.get("shown"):
            _box_broke(exc)  # the box call (or code right after it) failed: no box until restart
    if next_call is None:
        return request
    # Keep the provider call outside plugin exception recovery: never send twice.
    if acted:
        try:
            _remember_send(kwargs, time.time())
        except Exception:
            pass
    dispatch_ts = time.time()
    response = next_call()
    response_ts = time.time()
    try:
        if not isinstance(response, _SyntheticResponse):
            verdict = _record_response(kwargs, response, dispatch_ts, response_ts)
            if acted:
                _queue_outcome(kwargs, is_cli, verdict, bool(cfg.get("display_on_success")))
    except Exception:
        pass
    return response


_TRIM_TOLERANCE = 0.05  # a prompt this much smaller (or less) than the last is a trim, not a rewrite


def detect_observation(state: Dict[str, Any], attempt: Dict[str, Any]) -> Dict[str, Any]:
    """Build one immutable, domain-aware observation from a successful attempt."""
    session = state.get("session") or {}
    observations = state.get("observations") or []
    sid = attempt.get("session_id")
    model = attempt.get("model") or "unknown/unknown"
    api_mode = attempt.get("api_mode")
    last_api_ts = session.get("last_api_ts") if isinstance(session, dict) else None
    exact_session = (isinstance(session, dict) and isinstance(session.get("api_mode"), str)
                     and bool(session.get("api_mode")) and _finite_number(last_api_ts) and last_api_ts >= 0)
    previous = None
    if exact_session:
        for row in reversed(observations):
            if (isinstance(row, dict) and row.get("session_id") == sid
                    and row.get("model") == model and row.get("api_mode") == api_mode
                    and row.get("prompt_tokens") is not None and not _is_auxiliary(row)):
                previous = row.get("prompt_tokens")
                break

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
    elif attempt.get("prefix_changed") is True:  # the start of the chat is not what the last call sent
        cause = "rewrite"
    elif prompt is not None and prompt < previous and previous - prompt > _TRIM_TOLERANCE * previous:
        cause = "rewrite"
    else:
        cause = None

    gap = None
    if exact_session:
        try:
            dispatch_ts = attempt["dispatch_ts"]
            if _finite_number(dispatch_ts):
                gap = max(0.0, float(dispatch_ts) - float(last_api_ts))
        except (KeyError, TypeError, ValueError):
            pass
    if not exact_session:
        previous = None
    return {
        "ts": attempt.get("ts"), "session_id": sid, "model": model,
        "api_mode": api_mode, "api_request_id": attempt.get("api_request_id"),
        "gap_s": gap, "prompt_tokens": prompt, "cache_read_tokens": read,
        "cache_write_tokens": write, "prev_prompt_tokens": previous, "cause": cause,
    }


_COLD_SHARE = 0.30  # a call that reloads at least this share of the prompt lost the cache


def _cache_lost(reloaded: int, baseline: int) -> bool:
    """The one "cache lost" rule, for every turn."""
    return reloaded >= _COLD_SHARE * baseline


def _reloaded_on_change(row: Dict[str, Any]) -> Optional[int]:
    """Tokens a model-change turn had to reload ("up to": the new message was never cached),
    or None when the call reported no usage."""
    prompt, read = row.get("prompt_tokens"), row.get("cache_read_tokens")
    if _finite_number(prompt) and _finite_number(read) and prompt > 0:
        return max(0, int(prompt) - int(read))
    return None


def evaluate_observations(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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
        auxiliary = _is_auxiliary(source)
        evaluable = bool(known_domain and not auxiliary and source.get("cause") is None
                         and isinstance(expected, int) and not isinstance(expected, bool) and expected > 0
                         and isinstance(prompt, int) and not isinstance(prompt, bool) and prompt >= 0
                         and isinstance(read, int) and not isinstance(read, bool) and read >= 0)
        comparable = evaluable
        if evaluable and read == 0:
            domain = (source.get("model"), source.get("api_mode"))
            evaluable = positive_reads.get(domain, 0) >= 3
        delta = max(0, expected - read) if evaluable else None
        cold = _cache_lost(delta, expected) if evaluable else None
        if evaluable:  # never report more rebilled than was sent
            delta = max(0, min(delta, min(expected, prompt) - read))
        row.update(comparable=comparable, evaluable=evaluable, miss_delta=delta, cold=cold)
        result.append(row)
        if known_domain and not auxiliary:
            value = source.get("cache_read_tokens")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                domain = (source.get("model"), source.get("api_mode"))
                positive_reads[domain] = positive_reads.get(domain, 0) + 1
    return result


def _short_tokens(count: int) -> str:
    """23k, 999, 1.2M (nearest thousand; exact under 1,000)."""
    count = max(0, int(count))
    if count < 1000:
        return str(count)
    thousands = (count + 500) // 1000
    if thousands < 1000:
        return f"{thousands}k"
    return f"{count / 1_000_000:.1f}M"


def _compact_pause(gap_s: float) -> str:
    """45 s, 1 min, 2 h, 3 d (same steps as _pause_text)."""
    seconds = max(0, int(gap_s))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 180 * 60:
        return f"{seconds // 60} min"
    if seconds < 48 * 3600:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def _outcome_text(row: Dict[str, Any], change: Optional[Tuple[str, str]] = None) -> str:
    """This held attempt's measurement as one short line (the TUI status bar cuts at ~45 characters)."""
    gap = row.get("gap_s")
    pause = f", {_compact_pause(gap)}" if _finite_number(gap) and change is None else ""
    if change is not None:
        reloaded = _reloaded_on_change(row)
        if reloaded is None:
            return "cache unknown, model changed"
        if _cache_lost(reloaded, row["prompt_tokens"]):
            return f"cache lost, {_short_tokens(reloaded)} rebilled, model changed"
        return f"{_short_tokens(row['cache_read_tokens'])} reused, model changed"
    if not row["evaluable"]:
        reason = {
            "usage_missing": "usage unavailable",
            "model_switch": "model changed",
            "rewrite": "context changed",
            "auxiliary": "background task",
        }.get(row.get("cause"))
        if row.get("cause") == "start":
            return "cache unknown, first call"
        if reason is None:
            if row.get("prompt_tokens") is None or row.get("cache_read_tokens") is None:
                reason = "usage unavailable"
            elif (not row.get("api_mode") or row.get("api_mode") == "unknown"
                  or not isinstance(row.get("model"), str) or "/" not in row["model"]
                  or any(part in ("", "unknown") for part in row["model"].split("/", 1))):
                reason = "provider unknown"
            elif row.get("cache_read_tokens") == 0:
                reason = "too few cache hits"
            else:
                reason = "little history"
        return f"cache unknown, {reason}{pause}"
    if row["cold"]:
        return f"cache lost, {_short_tokens(row['miss_delta'])} rebilled{pause}"
    return f"{_short_tokens(row['cache_read_tokens'])} reused{pause}"


_PENDING: Dict[str, str] = {}  # session_id -> note to post after that session's next reply
_PENDING_TURN: Dict[str, str] = {}  # session_id -> turn_id the note belongs to (when known)
_PENDING_MAX = 64


def _queue_note(session_id: Any, text: str, turn_id: Any = None) -> None:
    if not isinstance(session_id, str) or not session_id:
        return
    if session_id not in _PENDING and len(_PENDING) >= _PENDING_MAX:
        oldest = next(iter(_PENDING))
        _PENDING.pop(oldest)
        _PENDING_TURN.pop(oldest, None)
    _PENDING[session_id] = text
    if isinstance(turn_id, str) and turn_id:
        _PENDING_TURN[session_id] = turn_id
    else:
        _PENDING_TURN.pop(session_id, None)


def _queue_outcome(kwargs: Dict[str, Any], is_cli: bool, row: Dict[str, Any],
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
        reloaded = _reloaded_on_change(row)
        reused = reloaded is not None and not _cache_lost(reloaded, row["prompt_tokens"])
    if reused and not display_on_success:
        return  # cache kept and nothing else to say; state above is already popped
    tag = "" if warned is None else ("OK " if reused else "WARN ")
    if change is None and _reports_no_cache(row):
        model = row.get("model")
        if model in _NO_CACHE_SAID:
            return  # said once per model and process; after that, silence
        _NO_CACHE_SAID.add(model)
        body = "cache unknown, model reports no cache"
    else:
        body = _outcome_text(row, change)
    _queue_note(sid, f"{_POST_PREFIX} {tag}{body}", kwargs.get("turn_id"))


_NO_CACHE_SAID: set = set()  # models already reported as "does not report a cache" (this process)


def _reports_no_cache(row: Dict[str, Any]) -> bool:
    """True when this model never reported a cache read: this call read 0 and it has never read more.

    Needs at least 3 such calls on record (the same count a zero read needs to be judged). Only
    mid-chat calls count: a first call or a model switch reads 0 on any provider.
    """
    if row.get("evaluable") or row.get("cause") is not None or row.get("cache_read_tokens") != 0:
        return False
    zero = 0
    for old in _load_observations():
        if old.get("model") != row.get("model") or old.get("api_mode") != row.get("api_mode"):
            continue
        read = old.get("cache_read_tokens")
        if _finite_number(read) and read > 0:
            return False
        zero += read == 0 and old.get("cause") is None
    return zero >= 3


_WARNED: Dict[str, str] = {}  # session_id -> warn-mode marker, shown with the result
_CHANGED: Dict[str, Tuple[str, str]] = {}  # session_id -> (previous, new) model of a model-change turn


def _without_old_lines(text: str) -> str:
    """Drop `[CACHEGUARD]` lines from a saved reply (history cleaning, see _clean_history).

    Old chats still hold such lines, written when cacheguard appended them to replies. Keep the
    strip: removing it would change their history and cost a cache rebill."""
    paras = text.split("\n\n")
    start = len(paras)
    while start > 0 and paras[start - 1].startswith(_PREFIX):
        start -= 1
    return "\n\n".join(paras[:start]) if start < len(paras) else text


_LOG = logging.getLogger(__name__)
_NOTICES_SEEN: Dict[str, set] = {}  # session_id -> one-off notices already posted to that session


def _post(text: str) -> bool:
    """Post one chat notice, outside the model context. Never raises; False if it was not posted."""
    try:
        handle = _CTX.post_message(text)
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
        if len(_NOTICES_SEEN) >= _PENDING_MAX:
            _NOTICES_SEEN.pop(next(iter(_NOTICES_SEEN)))
        seen = _NOTICES_SEEN[sid] = set()
    for reason in list(_H["pending_notices"]):
        if reason not in seen and _post(f"{_POST_PREFIX} {reason}"):
            seen.add(reason)


def _transform_llm_output(**kwargs: Any) -> Any:
    """Post the pending note as a chat notice (Hermes transform_llm_output). Never raises.

    The reply is never changed (None): one-off notices go first, then this turn's result line.
    Without ctx.post_message, or if a post fails, nothing is shown.
    """
    try:
        if _is_unattended(kwargs):
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


def _message_text(msg: Dict[str, Any]) -> Optional[str]:
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
    return text is not None and text.startswith(f"{_PREFIX} BLOCKED \u00b7 ")


def _is_plain_user(msg: Any) -> bool:
    """A user message that is typed text, not a tool result."""
    if not isinstance(msg, dict) or msg.get("role") != "user" or msg.get("tool_call_id"):
        return False
    content = msg.get("content")
    if isinstance(content, list):
        return not any(isinstance(p, dict) and ("result" in str(p.get("type")) or "output" in str(p.get("type")))
                       for p in content)
    return True


def _strip_note(msg: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of an assistant message without our trailing note; the same object if nothing to remove."""
    if msg.get("role") != "assistant":
        return msg
    content = msg.get("content")
    if isinstance(content, str):
        clean = _without_old_lines(content)
        return msg if clean == content or not clean else {**msg, "content": clean}
    if isinstance(content, list):
        for i in range(len(content) - 1, -1, -1):
            part = content[i]
            if _is_text_part(part):
                clean = _without_old_lines(part["text"])
                if clean == part["text"] or not clean:
                    return msg
                parts = list(content)
                parts[i] = {**part, "text": clean}
                return {**msg, "content": parts}
    return msg


def _clean_history(items: List[Any]) -> List[Any]:
    """The history without cacheguard lines: blocked pairs dropped, notes cut off replies."""
    out: List[Any] = []
    for item in items:
        if _is_blocked_reply(item):
            if out and _is_plain_user(out[-1]):
                out.pop()  # the question was never sent to the model
            continue
        out.append(_strip_note(item) if isinstance(item, dict) else item)
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


def _hash_messages(items: List[Any], n: int) -> Tuple[str, str]:
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


# session_id -> (api_request_id, message count, hash, prefix changed?) of the call being sent;
# saved to the session file once the call is answered (see _record_response)
_PREFIX_PENDING: Dict[str, Tuple[str, int, str, bool]] = {}
_PREFIX_PENDING_MAX = 64


def _note_prefix(kwargs: Dict[str, Any], clean: List[Any]) -> None:
    """Compare the start of this call's cleaned messages with the last answered call of the same chat."""
    sid = kwargs.get("session_id")
    if not isinstance(sid, str) or not sid:
        return
    payload = {**kwargs, "task_id": sid} if _is_minted_main_call(kwargs) else kwargs
    if _is_auxiliary(payload):
        return  # background calls neither read nor move the chat's hash
    rec = _get_session(sid)
    count, old = rec.get("prefix_n"), rec.get("prefix_hash")
    known = isinstance(count, int) and not isinstance(count, bool) and count > 0 and isinstance(old, str)
    head, whole = _hash_messages(clean, count if known else 0)
    changed = bool(known and (len(clean) < count or head != old))
    if sid not in _PREFIX_PENDING and len(_PREFIX_PENDING) >= _PREFIX_PENDING_MAX:
        _PREFIX_PENDING.pop(next(iter(_PREFIX_PENDING)))
    _PREFIX_PENDING[sid] = (str(kwargs.get("api_request_id") or ""), len(clean), whole, changed)


def _take_prefix(sid: str, api_request_id: str) -> Optional[Tuple[int, str, bool]]:
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
        if _is_unattended(kwargs):
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
                if len(clean) != len(items) or any(a is not b for a, b in zip(clean, items)):
                    return {"request": {**request, key: clean}, "source": "cacheguard",
                            "reason": "removed cacheguard lines"}
                return None
    except Exception:
        pass
    return None


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


def _gateway_confirmation(payload: Dict[str, Any], command: str, deny_text: str, model: str,
                          box: Optional[Dict[str, Any]] = None) -> Any:
    context = _gateway_approval()
    if context is not None:
        from tools.approval_gateway_wait import _await_gateway_decision

        key, notify = context
        pattern = "cacheguard:" + _domain_key(payload)
        if box is not None:
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
        if box is not None:
            box["denied"] = outcome
    else:
        outcome = "cancelled"
    return _denied_response(model, deny_text, outcome, payload.get("api_mode"))


def _single_query() -> bool:
    """Hermes' own marker for `chat -Q`, `--oneshot` and `-z`: nobody can answer a prompt."""
    try:
        from tools.approval_context import _is_single_query_approval_context

        return bool(_is_single_query_approval_context())
    except Exception:
        return os.environ.get("HERMES_SINGLE_QUERY_SESSION", "").strip().lower() in ("1", "true", "yes", "on")


def _act(kwargs: Dict[str, Any], is_cli: bool, mode: Any,
         warn_note: str, command: str, deny_text: Any, model: str,
         box: Optional[Dict[str, Any]] = None) -> Any:
    """Apply a mode ("off" | "warn" | "block"). Return a synthetic response to
    end the turn, else None (the caller sends normally).

    A warning is not printed: warn_note ("" or a short reason) marks the session so the
    result line after the reply starts with `WARN ·` (else `OK ·`).

    `box` is the caller's per-call marker: it gets "shown" once the question went to Hermes
    and "denied" once a no-send answer is known.
    """
    if mode == "off":
        return None
    shown = False
    if mode == "block" and _H.get("box_failed"):
        mode = "warn"  # the box failed earlier in this process: warn until Hermes restarts
    if mode == "block" and is_cli and not _healthy("approval_cb"):
        mode = "warn"  # degraded: no approval callback in this Hermes
    if mode == "block" and not is_cli and _gateway_approval() is None:
        mode = "warn"  # degraded: no way to ask in this chat
        _notice(_NO_BOX_TEXT)
    if mode == "block" and _single_query():
        mode = "warn"  # `chat -Q` / `-z`: no screen, a prompt would only wait out the timeout
    if mode == "block" and is_cli and _api_mode_supported(kwargs):
        shown, blocked = _confirmation_screen(kwargs, command, deny_text, model, box)
        if not shown and _gateway_approval() is not None:
            # The TUI (and desktop) registers no CLI callback; it asks through the gateway queue.
            blocked = _gateway_confirmation(kwargs, command, deny_text, model, box)
            shown = True
        if blocked is not None:
            return blocked
    if mode == "block" and not is_cli and _api_mode_supported(kwargs):
        blocked = _gateway_confirmation(kwargs, command, deny_text, model, box)
        shown = True
        if blocked is not None:
            return blocked
    if not shown and isinstance(kwargs.get("session_id"), str) and kwargs.get("session_id"):
        # No panel was shown (the panel already carries the info): say it with the result line.
        _WARNED[kwargs["session_id"]] = warn_note
    return None


def _domain_key(payload: Dict[str, Any]) -> str:
    return json.dumps([_model_key(payload.get("provider"), payload.get("model")),
                       payload.get("api_mode") or "unknown"])


def _guard_sends(rec: Dict[str, Any]) -> Dict[str, Any]:
    """The saved send times; a wrong type in the session file counts as empty."""
    sends = rec.get("guard_sends")
    return sends if isinstance(sends, dict) else {}


def _remember_send(payload: Dict[str, Any], sent_at: float) -> None:
    sid = str(payload.get("session_id") or "")
    with _storage_lock():
        rec = _read_session(sid)
        sends = dict(_guard_sends(rec))
        sends[_domain_key(payload)] = sent_at
        _write_json_atomic(_session_file(sid), dict(rec, guard_sends=sends))


def _cooldown_active(rec: Dict[str, Any], payload: Dict[str, Any], cfg: Dict[str, Any], now: float) -> bool:
    sent_at = _guard_sends(rec).get(_domain_key(payload))
    return _finite_number(sent_at) and 0 <= now - sent_at < _pause_seconds(cfg["pause_m"])  # future time: clock went back


def _evaluate_model_change(kwargs: Dict[str, Any], cfg: Dict[str, Any], surface_mode: Any = None) -> Optional[Tuple[int, str, str]]:
    """Return (size, previous_model, model) when the session's model/provider changed."""
    surface_mode = surface_mode if surface_mode is not None else _surface_mode(kwargs, cfg)
    if surface_mode not in ("warn", "block"):
        return None
    if int(kwargs.get("api_call_count") or 0) != 1:
        return None
    session_id = kwargs.get("session_id") or ""
    model = _model_key(kwargs.get("provider"), kwargs.get("model"))
    rec = _get_session(session_id)
    prev = rec.get("model") if isinstance(rec.get("model"), str) else None
    mode_changed = (isinstance(rec.get("api_mode"), str) and kwargs.get("api_mode")
                    and rec["api_mode"] != kwargs["api_mode"])
    if not prev or (prev == model and not mode_changed):
        return None
    if _cooldown_active(rec, kwargs, cfg, time.time()):
        return None
    if mode_changed:
        prev = f"{prev} ({rec['api_mode']} → {kwargs['api_mode']})"
    size = score_risk(rec, {"request": kwargs.get("request")})["tokens_at_risk"]
    if size < _size_tokens(cfg["size_k"]):
        return None
    return size, prev, model


def _evaluate(kwargs: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None, surface_mode: Any = None) -> Optional[Tuple[int, str, float]]:
    """Return (size, model, gap_s) when the trigger fires, else None."""
    cfg = cfg or _cfg()
    surface_mode = surface_mode if surface_mode is not None else _surface_mode(kwargs, cfg)
    # Only the first call of a turn: the read that pays the cold context.
    if int(kwargs.get("api_call_count") or 0) != 1:
        return None
    if surface_mode not in ("warn", "block"):  # block without an approval path acts like warn (see _act)
        return None

    session_id = kwargs.get("session_id") or ""
    model = _model_key(kwargs.get("provider"), kwargs.get("model"))

    rec = _get_session(session_id)
    now = time.time()
    if _cooldown_active(rec, kwargs, cfg, now):
        return None
    risk = score_risk(rec, {
        "request": kwargs.get("request"), "now": now, "settings": cfg,
    })
    factors = risk["factors"]
    if not factors["window_crossed"] or not factors["floor_crossed"]:
        return None
    size, gap_s = risk["tokens_at_risk"], factors["gap_s"]

    return size, model, gap_s


def _pause_text(gap_s: float) -> str:
    seconds = max(0, int(gap_s))
    if seconds < 60:
        count, unit = seconds, "second"
    elif seconds < 180 * 60:
        count, unit = seconds // 60, "minute"
    elif seconds < 48 * 3600:
        count, unit = seconds // 3600, "hour"
    else:
        count, unit = seconds // 86400, "day"
    return f"{count} {unit}{'' if count == 1 else 's'}"


def _cache_cost_text(size: int, gap_s: float, cold: Optional[Tuple[int, int]] = None) -> str:
    """The confirm box text. cold: (lost, calls) of past calls after this pause, if known."""
    return (f"{_PREFIX} Possible cache cost\n\n"
            f"Paused {_pause_text(gap_s)} · ~{size:,} tokens may be rebilled"
            + (f" · lost {cold[0]} of {cold[1]} similar pauses" if cold is not None else ""))


def _cold_counts(model: str, gap_s: float) -> Optional[Tuple[int, int]]:
    """(lost, calls): past calls of this model that found a cold cache after a similar pause.

    None until the matching pause bucket holds _MIN_CHANCE_CALLS measurements.
    """
    try:
        rows = _load_observations()
        cells = _cold_cells(rows, evaluate_observations(rows)).get(model)
        cell = cells[_bucket_index(float(gap_s), _GAP_EDGES)] if cells else None
        if cell and cell["calls"] >= _MIN_CHANCE_CALLS:
            return cell["cold"], cell["calls"]
    except Exception:
        pass
    return None


def _confirmation_screen(kwargs: Dict[str, Any], command: str, deny_text: str, model: str,
                         box: Optional[Dict[str, Any]] = None) -> Tuple[bool, Any]:
    """Ask Send message/Cancel before sending; cancellation returns a synthetic response.

    A missing callback, or an error before the question reaches Hermes, falls back to a
    warning. An error in the box call sends the message and switches the box off. Only
    once/session/always authorize the request; no choice cancels, and a known Cancel stays
    "not sent" even if the plugin fails afterwards.
    """
    try:
        from tools.terminal_tool import _get_approval_callback

        cb = _get_approval_callback()
    except Exception:
        cb = None
    if cb is None:
        return False, None  # no human prompt available -> warn path

    shown = False  # set right before the question goes to Hermes
    outcome = None  # set as soon as a no-send answer is known
    try:
        owner = getattr(cb, "__self__", None)
        modal = getattr(owner, "_prompt_text_input_modal", None)
        lock = getattr(owner, "_approval_lock", None)
        if callable(modal) and getattr(owner, "_app", None) is not None and lock is not None:
            # Use Hermes' existing TUI confirmation, serialized with tool approvals.
            # No app means the native helper falls back to raw stdin; use the
            # registered approval callback in that case instead.
            timeout = _approval_timeout()
            title, _, detail = command.partition("\n\n")
            choices = [("once", "Send message", "send it now"), ("deny", "Cancel", "do not send")]
            with lock:
                shown = True
                if box is not None:
                    box["shown"] = True
                answer = modal(title=title, detail=detail, choices=choices, timeout=timeout)
            # The modal returns None when the timeout runs out, "deny" for Cancel.
            answer = _box_answer(owner, answer, choices)
        else:
            shown = True
            if box is not None:
                box["shown"] = True
            answer = cb(command, "Possible cache cost", allow_permanent=False, allow_session=False)
            if answer is not None and answer not in ("once", "session", "always", "deny", "timeout"):
                raise ValueError("unexpected box answer")  # not a value Hermes gives: a box failure
        if answer not in ("once", "session", "always"):  # Hermes returns "timeout" (no answer) after printing "denying command"
            outcome = "timeout" if answer == "timeout" else "cancelled"
            if box is not None:
                box["denied"] = outcome
            return True, _denied_response(model, deny_text, outcome, kwargs.get("api_mode"))
    except Exception as exc:
        if not shown:
            return False, None
        if outcome is not None:  # the answer was a no-send: stay "not sent" if we can
            return True, _denied_response(model, deny_text, outcome, kwargs.get("api_mode"))
        _box_broke(exc)
        return True, None
    return True, None


_NOT_SENT_TEXT = _PREFIX + " BLOCKED · message not sent"
_BOX_OFF_TEXT = ("the cacheguard box failed, so messages now go out with a warning "
                 "until Hermes restarts. Details: /cacheguard")


def _box_broke(exc: BaseException) -> None:
    """The box call failed: switch the box off for this process and say so once."""
    _H["box_failed"] = type(exc).__name__
    _notice(_BOX_OFF_TEXT)


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


def _box_answer(owner: Any, answer: Any, choices: Any) -> str:
    """Map the box answer to once, deny, timeout or unknown. Only once sends.

    Typed answers are read by Hermes' own helper against the two choices the box shows
    (Send message, Cancel). Without the helper, only the exact choice ids count. An answer
    nobody can read is typed text, not a Send: it prints one plain line and the message is not
    sent. An answer that is not text at all means the box changed shape: it raises, the caller
    treats it as a box failure (message sent, box off).
    """
    if answer is None:
        return "timeout"
    if not isinstance(answer, str):
        raise ValueError("unexpected box answer")  # not text at all: a box failure, not a human answer
    normalized = answer if answer in ("once", "deny") else None  # exact match, no helper needed
    normalize = getattr(owner, "_normalize_slash_confirm_choice", None)
    if callable(normalize) and isinstance(answer, str):
        try:
            # "cancel" is not one of our ids, but a typed no/n/cancel is a clear Cancel.
            normalized = normalize(answer, list(choices) + [("cancel", "Cancel", "do not send")])
        except Exception:
            pass
    if normalized == "once":
        return "once"
    if normalized in ("deny", "cancel"):
        return "deny"
    _notice("unreadable answer in the cacheguard box: message not sent")
    return "unknown"


def _approval_timeout() -> int:
    """Seconds Hermes waits for a confirmation: its own (capped) value, else approvals.timeout."""
    try:
        from tools.approval_context import _get_approval_timeout

        return int(_get_approval_timeout())
    except Exception:
        pass
    try:
        return int((_raw_config().get("approvals") or {}).get("timeout", 300))
    except Exception:
        return 300


def _denied(deny_text: Any, outcome: str) -> str:
    """The deny text; a callable builds it once we know if the user cancelled or timed out."""
    return deny_text(outcome, _approval_timeout()) if callable(deny_text) else deny_text


def _deny_text(session_id: Optional[str] = None, gateway: bool = False, gap_s: Optional[float] = None,
               outcome: str = "cancelled", timeout: Optional[int] = None, cause: Optional[str] = None) -> str:
    text = _PREFIX + " BLOCKED · message not sent"
    if _finite_number(gap_s):
        text += f" after a pause of {_pause_text(gap_s)}"
    if outcome == "timeout" and timeout is not None:
        text += (" and " if _finite_number(gap_s) else " after ") + f"{timeout}s timeout"
    elif outcome == "undelivered":
        text += (" and " if _finite_number(gap_s) else " · ") + "the prompt could not be delivered"
    elif _finite_number(gap_s):
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


def _api_mode_supported(kwargs: Dict[str, Any]) -> bool:
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


# --------------------------------------------------------------------------- #
# middleware return: learn from real usage
# --------------------------------------------------------------------------- #
def _record_response(payload: Dict[str, Any], response: Any,
                     dispatch_ts: float, response_ts: float) -> Dict[str, Any]:
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
        "ts": response_ts, "dispatch_ts": dispatch_ts, "session_id": sid,
        "model": _model_key(payload.get("provider"), payload.get("model")),
        "api_mode": str(payload.get("api_mode") or "unknown"),
        "api_request_id": str(payload.get("api_request_id") or ""),
        "prompt_tokens": prompt, "cache_read_tokens": cached, "cache_write_tokens": written,
    }
    auxiliary = _is_auxiliary(payload)
    prefix = None if auxiliary else _take_prefix(sid, attempt["api_request_id"])
    if prefix is not None:
        attempt["prefix_changed"] = prefix[2]
    with _storage_lock():
        rows, truncated = _read_recent_observations()  # the newest rows: enough for this call
        rec = _read_session(sid)
        if auxiliary:
            row = detect_observation({"session": {}, "observations": []}, attempt)
            row["cause"] = "auxiliary"
        else:
            row = detect_observation({"session": rec, "observations": rows}, attempt)
            if truncated and row["cause"] == "start" and row["gap_s"] is not None:
                # A known session (it has a gap) whose previous row is older than the tail: look through everything.
                row = detect_observation({"session": rec, "observations": _read_observations()}, attempt)
        task_id = payload.get("task_id")
        if isinstance(task_id, str) and task_id:
            row["task_id"] = task_id
        _write_observation(row)
        _maybe_trim_observations(_MAX_OBSERVATIONS)
        if not auxiliary:
            updated = dict(rec, last_api_ts=response_ts, model=attempt["model"], api_mode=attempt["api_mode"])
            if prompt is not None:
                updated["prompt_tokens"] = prompt
            if prefix is not None:
                updated["prefix_n"], updated["prefix_hash"] = prefix[0], prefix[1]
            _write_json_atomic(_session_file(sid), updated)
        elif (attempt["model"] == rec.get("model") and attempt["api_mode"] == rec.get("api_mode")
              and _finite_number(rec.get("prompt_tokens")) and rec["prompt_tokens"] > 0
              and cached is not None and cached >= 0.70 * rec["prompt_tokens"]):
            # A background call that read most of the chat kept the cache warm: restart
            # the guard's idle clock only. Stats keep measuring pauses between main calls.
            _write_json_atomic(_session_file(sid), dict(rec, cache_touch_ts=response_ts))
    # A display failure must not lose the row or replay the provider call.
    verdict = evaluate_observations(rows + [row])[-1]
    if truncated and verdict["comparable"] and not verdict["evaluable"]:
        # A read of 0 counts only after 3 positive reads of the same model: they may be older than the tail.
        try:
            verdict = evaluate_observations(_read_observations() + [row])[-1]
        except Exception:
            pass
    return verdict




# --------------------------------------------------------------------------- #
# /cacheguard — show the current knowledge
# --------------------------------------------------------------------------- #
_CTX: Any = None  # plugin context, kept for /cacheguard set (ctx.set_config)

_SET_USAGE = (
    "usage: /cacheguard set <name> <value>\n"
    "  cli           off | warn | block   terminal, editor\n"
    "  desktop       off | warn | block   desktop app, web UI\n"
    "  gateway       off | warn | block   Telegram and other messaging platforms\n"
    "  pause_m       number >= 1       alert after this many minutes of pause\n"
    "  size_k        number (in k)     alert from this prompt size, e.g. 30 = 30k (max 2000)\n"
    "  display_on_success  on | off    show the result line when the cache was kept (default off)"
)


_SETUP_USAGE = (
    "usage: /cacheguard setup <all|cli|desktop|gateway> <off|warn|block> [pause_m] [size_k]\n"
    "  /cacheguard setup all off                   cli, desktop and gateway: off (pause and size unchanged)\n"
    "  /cacheguard setup all block 10 30           cli, desktop and gateway: block\n"
    "  /cacheguard setup gateway warn 10 30        gateway: warn (even if it was off)\n"
    "  Give both pause and size, or neither. Pause and size apply to all groups."
)

_HELP_TEXT = (
    "cacheguard help\n"
    "\n"
    "Commands\n"
    "  /cacheguard                          summary: how often the cache was lost\n"
    "  /cacheguard all                      every recorded model\n"
    "  /cacheguard <model>                  only models whose name contains this text\n"
    "  /cacheguard setup <all|cli|desktop|gateway> <mode> [pause_m] [size_k]\n"
    "                                       one group, or all three. Pause and size apply to all groups:\n"
    "                                       give them together, or leave them out to keep the current values\n"
    "  /cacheguard set <name> <value>\n"
    + "\n".join(_SET_USAGE.splitlines()[1:]) + "\n"
    "  /cacheguard export <first> <last>    save the first and last messages to a file (CLI only)\n"
    "  /cacheguard export all               save every message (CLI only)\n"
    "  /cacheguard debug                    recent raw records\n"
    "  /cacheguard reset                    delete saved history\n"
    "  /cacheguard help                     this text\n"
    "\n"
    "Examples (copy and paste)\n"
    "  /cacheguard setup all off            cli, desktop and gateway: off (pause and size unchanged)\n"
    "  /cacheguard setup cli block 10 20    cli: block after a 10 minute pause, prompts of 20k+\n"
    "  /cacheguard setup gateway warn 10 30 gateway warns, even if it was off\n"
    "  /cacheguard set gateway block        guard Telegram and other messaging platforms\n"
    "  /cacheguard set pause_m 1            change only the pause, here 1 minute\n"
    "  /cacheguard export 5 5               save the first and last 5 messages (CLI only)"
)


def _parse_setting(name: str, raw: str) -> Tuple[Optional[Tuple[str, Any, str]], str]:
    """Validate one `name value` pair. Return ((config_key, value, shown), "") or (None, error)."""
    name, raw = name.lower(), raw.lower()
    if name in ("cli", "desktop", "gateway"):
        allowed = ("off", "warn", "block")
        if raw not in allowed:
            return None, f"cacheguard: {name} must be one of {', '.join(allowed)}."
        return (name, raw, raw), ""
    if name == "display_on_success":
        if raw not in ("on", "off"):
            return None, "cacheguard: display_on_success must be on or off."
        return (name, raw == "on", raw), ""
    if name in ("pause_m", "size_k"):
        try:
            number = float(raw)
        except ValueError:
            number = math.nan
        if not _valid_number(name, number):
            if name == "pause_m":
                return None, "cacheguard: pause_m must be a number >= 1."
            return None, "cacheguard: size_k is in thousands: 20 = 20k. A number from 0 to 2000."
        saved = int(number) if number.is_integer() else number
        return (name, saved, f"{number:g} min" if name == "pause_m" else f"{number:g}k"), ""
    return None, ""


_PLUGIN_ID = "cacheguard"


def _save_setting(key: str, value: Any) -> None:
    """Write one setting. In a session: through the plugin context. In the terminal (no context):
    straight to config.yaml, using the same writer Hermes uses for plugin settings."""
    if _CTX is not None and hasattr(_CTX, "set_config"):
        _CTX.set_config(key, value)
        return
    from hermes_cli.plugins_state import save_plugin_setting
    save_plugin_setting(_PLUGIN_ID, (key,), value)


def _write_settings(items: List[Tuple[str, Tuple[str, Any, str]]]) -> str:
    """Write validated settings; one summary line per setting."""
    lines = []
    for name, (key, value, shown) in items:
        try:
            _save_setting(key, value)
        except Exception as exc:
            lines.append(f"cacheguard: could not write {name} ({exc}).")
            break
        lines.append(f"cacheguard: {name} = {shown} (saved to config).")
    return "\n".join(lines)


def _cmd_set(args: List[str]) -> str:
    if len(args) != 2:
        return _SET_USAGE
    parsed, err = _parse_setting(args[0], args[1])
    if parsed is None:
        return (err + "\n" if err else "") + _SET_USAGE
    return _write_settings([(args[0].lower(), parsed)])


def _cmd_setup(args: List[str]) -> str:
    """/cacheguard setup <all|cli|desktop|gateway> <mode> [pause_m] [size_k]: the main settings at once.

    all: all three groups get the mode. One group: only that group.
    Minutes and tokens are global and optional: give both or neither. Omitted = left unchanged.
    Nothing is written unless every given value is valid.
    """
    if len(args) < 2 or len(args) > 4:
        return _SETUP_USAGE
    if args[0].lower() not in ("all",) + _SETUP_GROUPS:
        return f"cacheguard: Unknown group {args[0]}; use all, cli, desktop or gateway.\n" + _SETUP_USAGE
    if len(args) == 3:
        return "cacheguard: Give both pause and size, or omit both.\n" + _SETUP_USAGE
    target, mode_raw = args[0].lower(), args[1]
    if mode_raw.lower() not in ("off", "warn", "block"):
        return "cacheguard: mode must be off, warn or block.\n" + _SETUP_USAGE
    groups = list(_SETUP_GROUPS) if target == "all" else [target]
    items = [(g, _parse_setting(g, mode_raw)[0]) for g in groups]
    with_timing = len(args) == 4
    if with_timing:
        pause_item, err = _parse_setting("pause_m", args[2])
        if pause_item is None:
            return err + "\n" + _SETUP_USAGE
        size_item, err = _parse_setting("size_k", args[3])
        if size_item is None:
            return err + "\n" + _SETUP_USAGE
        items += [("pause_m", pause_item), ("size_k", size_item)]
    for name, (key, value, _shown) in items:
        try:
            _save_setting(key, value)
        except Exception as exc:
            return f"cacheguard: could not write {name} ({exc})."
    modes = " · ".join(f"{g} = {mode_shown}" for g, (_key, _value, mode_shown) in items[:len(groups)])
    if not with_timing:
        return f"cacheguard: {modes} (saved to config)."
    shown = {name: parsed[2] for name, parsed in items}
    return (f"cacheguard: {modes} · pause ≥ {shown['pause_m']} · size ≥ {shown['size_k']} "
            "(pause and size apply to all groups; saved to config).")


_SETUP_GROUPS = ("cli", "desktop", "gateway")


def _active_cli_session_id() -> Optional[str]:
    """Return the session open in the interactive CLI at command time."""
    cli = getattr(getattr(_CTX, "_manager", None), "_cli_ref", None)
    session_id = getattr(cli, "session_id", None)
    return session_id if isinstance(session_id, str) and session_id.strip() else None

_EXPORT_USAGE = (
    "usage: /cacheguard export <first> <last>   or   /cacheguard export all\n"
    "  example: /cacheguard export 5 5   (first 5 and last 5 messages)\n"
    "  Writes a Markdown file, no model call, no tokens."
)


def _export_messages(session_id: str) -> List[Tuple[str, str]]:
    import sqlite3

    from hermes_constants import get_hermes_home

    con = sqlite3.connect(_state_db_uri(get_hermes_home()), uri=True, timeout=1.0)
    try:
        rows = con.execute(
            "SELECT role, content FROM messages WHERE session_id = ? "
            "AND role IN ('user', 'assistant') AND content IS NOT NULL AND content != '' "
            "AND " + _NOT_DENY_SQL + " "
            "ORDER BY timestamp, id",
            (session_id,),
        ).fetchall()
    finally:
        con.close()
    return [(r, c) for r, c in rows]


def _gateway_session_id() -> Optional[str]:
    """Hermes session of the chat that sent a gateway slash command, via its routing key."""
    try:
        import sqlite3

        from gateway.session_context import get_session_env
        from hermes_constants import get_hermes_home

        key = get_session_env("HERMES_SESSION_KEY", "")
        if not key:
            return None
        con = sqlite3.connect(_state_db_uri(get_hermes_home()), uri=True, timeout=1.0)
        try:
            row = con.execute(
                "SELECT id FROM sessions WHERE session_key = ? ORDER BY started_at DESC LIMIT 1", (key,)
            ).fetchone()
        finally:
            con.close()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def _gateway_export_hint() -> str:
    """Chat apps cannot save a file; point to the recovery step the CLI deny text gives."""
    session_id = _gateway_session_id()
    if not session_id:
        return ("cacheguard: no chat to export here (the file export works only in the Hermes CLI).\n"
                "To keep this chat: type `/new`, then ask Hermes to read back the previous session.")
    escaped_id = json.dumps(str(session_id), ensure_ascii=False)
    return ("cacheguard: the file export works only in the Hermes CLI. To keep this chat:\n"
            "1. Start fresh: `/new`\n"
            f"2. Then paste: `Read back session {escaped_id} and continue from there.`")


def _cmd_export(args: List[str]) -> str:
    """/cacheguard export <first> <last> | all: dump the chat to a Markdown file (no LLM call)."""
    if len(args) == 1 and args[0].lower() == "all":
        first = last = None
    elif len(args) == 2 and all(a.isdecimal() for a in args) and any(int(a) for a in args):
        first, last = int(args[0]), int(args[1])
    else:
        return _EXPORT_USAGE
    session_id = _active_cli_session_id()
    if not session_id:
        return _gateway_export_hint()
    try:
        msgs = _export_messages(session_id)
    except Exception as exc:
        return f"cacheguard: could not read the chat ({exc})."
    if not msgs:
        return "cacheguard: nothing to export (no user/assistant messages found)."

    total = len(msgs)
    if first is None or first + last >= total:
        parts = [(i + 1, m) for i, m in enumerate(msgs)]
        omitted = 0
    else:
        parts = [(i + 1, m) for i, m in enumerate(msgs[:first])]
        omitted = total - first - last
        parts += [(None, None)] + [(total - last + i + 1, m) for i, m in enumerate(msgs[total - last:])]

    out = [f"# Chat export ({session_id})", ""]
    for num, m in parts:
        if num is None:
            out += [f"*... {omitted} messages omitted ...*", ""]
            continue
        out += [f"## {num}. {m[0]}", "", m[1], ""]
    try:
        d = _state_dir() / "exports"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{_safe_name(session_id)}-{int(time.time())}.md"
        _write_private_text(path, "\n".join(out))
    except Exception as exc:
        return f"cacheguard: could not write the file ({exc})."
    kept = total - omitted
    return f"cacheguard: {kept} of {total} messages saved to\n{path}\nStart /new, then paste this path or its content."


def _cmd_debug(args: List[str]) -> str:
    """Dev tool: per-call timeline of the last N sessions (model switches, cold reads)."""
    n = int(args[0]) if args and args[0].isdecimal() else 3
    sessions: Dict[str, List[Dict[str, Any]]] = {}
    raw_rows = _load_observations()
    measured = evaluate_observations(raw_rows)
    for result in measured:
        r = dict(result)
        sessions.setdefault(str(r.get("session_id") or "?"), []).append(r)
    recent = sorted(sessions.items(), key=lambda kv: (
        kv[1][-1].get("ts") if _finite_number(kv[1][-1].get("ts")) else 0
    ))[-n:]
    if not recent:
        return "cacheguard debug: no observations yet."
    out: List[str] = []
    for sid, rows in recent:
        out.append(f"session {sid}  ({len(rows)} calls)")
        prev_model = None
        for r in rows:
            prompt_raw = r.get("prompt_tokens")
            prompt = int(prompt_raw) if isinstance(prompt_raw, int) and not isinstance(prompt_raw, bool) else None
            cached_raw = r.get("cache_read_tokens")
            cached_label = (f"{int(cached_raw) * 100 // prompt:>3d}%"
                            if isinstance(cached_raw, int) and prompt else "?")
            gap = r.get("gap_s")
            flags = []
            if prev_model and r.get("model") != prev_model:
                flags.append(f"MODEL CHANGE from {prev_model}")
            if r["evaluable"]:
                flags.append("COLD" if r["cold"] else "OK")
            else:
                flags.append("NON-EVALUABLE")
            prev_model = r.get("model")
            when = (time.strftime("%m-%d %H:%M:%S", time.localtime(r["ts"]))
                    if _finite_number(r.get("ts")) else "unknown-time")
            prompt_label = f"{prompt // 1000:>4d}k" if prompt is not None else "    ?"
            out.append(
                f"  {when}  gap {_pause_text(gap) if gap is not None else 'first':>10s}  "
                f"{prompt_label}  cached {cached_label:>4s}  "
                f"{str(r.get('model') or 'unknown')}  {' '.join(flags)}".rstrip()
            )
    return "\n".join(out)


# Surfaces known to render a fenced code block as monospace text.
_MARKDOWN_SURFACES = frozenset({"telegram", "discord", "slack", "matrix", "mattermost", "webui"})


def _markdown_surface() -> bool:
    """True when this command reply goes to a Markdown renderer (fence the table), False for raw text.

    Raw text: classic CLI, TUI (plain text pager), desktop app (plain text), and any surface not
    known to render a code fence. Chat apps and the web UI render Markdown. The web UI runs the
    command in its own server process with no session bound, so it is told apart by its
    `api.commands` module being loaded and the terminal flag (HERMES_INTERACTIVE) being unset.
    """
    idents = [os.environ.get("HERMES_PLATFORM", ""), _session_env("HERMES_SESSION_PLATFORM"),
              _session_env("HERMES_SESSION_SOURCE")]
    idents = [str(ident).strip().lower() for ident in idents if str(ident).strip()]
    if idents:
        return any(ident in _MARKDOWN_SURFACES for ident in idents)
    if os.environ.get("HERMES_INTERACTIVE", "").strip().lower() in ("1", "true", "yes", "on"):
        return False  # classic CLI (and the TUI backend before it binds a session)
    webui = sys.modules.get("api.commands")
    return callable(getattr(webui, "execute_plugin_command", None))


def _unknown_argument(word: str) -> str:
    return f"unknown argument '{word}'. Try /cacheguard help"


def _cmd_show(raw_args: str = "", terminal: bool = False) -> str:
    """The /cacheguard command. terminal=True: `hermes cacheguard` (no chat): the summary names
    the terminal command."""
    parts = (raw_args or "").split()
    if parts and parts[0].lower() == "help":
        return _HELP_TEXT if len(parts) == 1 else _unknown_argument(parts[1])
    if parts and parts[0].lower() == "debug":
        if len(parts) > 2 or (len(parts) == 2 and not parts[1].isdecimal()):
            return _unknown_argument(parts[1] if not parts[1].isdecimal() else parts[2])
        return _cmd_debug(parts[1:])
    if parts and parts[0].lower() == "set":
        return _cmd_set(parts[1:])
    if parts and parts[0].lower() == "setup":
        return _cmd_setup(parts[1:])
    if parts and parts[0].lower() == "export":
        return _cmd_export(parts[1:])
    if parts and parts[0].lower() == "reset":
        if len(parts) > 1:
            return "usage: /cacheguard reset"
        try:
            import shutil

            global _obs_cache
            with _storage_lock():
                _obs_path().unlink(missing_ok=True)
                shutil.rmtree(_state_dir() / "sessions", ignore_errors=True)
                _obs_cache = None
            return "cacheguard: Measurements and session tracking cleared. Settings and exported chats kept."
        except Exception as exc:  # pragma: no cover
            return f"cacheguard: could not clear observations ({exc})."

    # No subcommand: summary. "all" = every row. A word naming a recorded model = model filter.
    word = parts[0].lower() if parts else ""
    if len(parts) > 1:
        return _unknown_argument(parts[1])
    rows = _load_observations()
    if word and word != "all" and not any(word in str(row.get("model") or "unknown").lower() for row in rows):
        return f"No recorded model matches {parts[0]}. Use /cacheguard all to list recorded models."
    show_all = word == "all"
    model_filter = "" if show_all else word

    cfg = _cfg()
    evaluated = evaluate_observations(rows)
    labels = _GAP_LABELS

    minutes = f"{cfg['pause_m']:g}"
    kilo = f"{cfg['size_k']:g}"
    models = _cold_cells(rows, evaluated, model_filter)
    selected = sorted(
        ((model, cells) for model, cells in models.items() if any(cell["calls"] for cell in cells)),
        key=lambda pair: (-sum(cell["calls"] for cell in pair[1]), pair[0]))
    shown = selected if (show_all or model_filter) else selected[:_TOP_ROWS]
    measured = sum(cell["calls"] for _, cells in selected for cell in cells)
    # Same counts as the table cells below (cell "n" or the "/calls" denominator), for the shown rows.
    in_table = sum(cell["calls"] for _, cells in shown for cell in cells)
    pauses = f"{in_table:,} measured pause{'' if in_table == 1 else 's'}"
    cmd = "hermes cacheguard" if terminal else "/cacheguard"
    lines = [
        f"cacheguard  ·  cli {cfg['cli']}  ·  desktop {cfg['desktop']}  ·  gateway {cfg['gateway']}"
        f"  ·  pause {_pause_text(_pause_seconds(cfg['pause_m']))}"
        f"  ·  size {cfg['size_k']:g}k  ·  {pauses}",
        "",
        f"setup: {cmd} setup all block {minutes} {kilo}  (cli, desktop and gateway: block after a {minutes} minute pause, size over {kilo}k tokens)",
        f"set: {cmd} set pause_m 1  (change only the pause, here 1 minute)",
        f"help: {cmd} help",
        "",
    ]
    if _health_text():  # nothing to say when all is well
        lines += [_health_text(), ""]
    lines.append("Cache lost, by pause length")
    if models and 0 < measured < _MIN_TABLE_CALLS:
        lines.append(f"({measured} measurements so far, need at least {_MIN_TABLE_CALLS})")
        return "\n".join(lines)
    try:
        suggested = _suggested_pauses(evaluated, model_filter)
    except Exception:
        suggested = {}
    labels = labels + ["suggested"]
    rendered_rows = []
    for model, cells in shown:
        values = []
        for cell in cells:
            if not cell["calls"]:
                values.append("—")
                continue
            values.append(f"{cell['cold']}/{cell['calls']}")
        values.append(suggested.get(model, "—"))
        rendered_rows.append((model, values))
    model_width = max([len("model")] + [len(model) for model, _ in rendered_rows])
    widths = [max([len(label)] + [len(values[i]) for _, values in rendered_rows])
              for i, label in enumerate(labels)]
    table = [f"{'model':<{model_width}}  " + "  ".join(
        label.rjust(width) for label, width in zip(labels, widths))]
    for model, values in rendered_rows:
        table.append(f"{model:<{model_width}}  " + "  ".join(
            value.rjust(width) for value, width in zip(values, widths)))
    # A Markdown renderer folds the spaces and loses the columns; a fence keeps them.
    lines += ["```", *table, "```"] if _markdown_surface() else table
    if not models:
        lines.append(f"(no observations for '{model_filter}')" if model_filter
                     else "(no observations yet)")
    elif not selected:
        lines.append("(no usable pause observations yet; pauses under 1 min are not shown)")
    if len(shown) < len(selected):
        lines.append(f"... {len(selected) - len(shown)} more models: /cacheguard all"
                     + (" (in a Hermes chat)" if terminal else ""))
    return "\n".join(lines)



def _cmd_entry(raw_args: str = "") -> str:
    """/cacheguard output with blank lines as one space: the Hermes TUI pager drops
    empty lines (NousResearch/hermes-agent#30782); the CLI shows both the same."""
    return "\n".join(line or " " for line in _cmd_show(raw_args).split("\n"))

_TOP_ROWS = 10
# Edges sit 1 minute past 5 min, 20-min steps and 1 h: the measured pause runs a
# bit longer than the time the provider counts.
_GAP_EDGES = [360.0, 1260.0, 2460.0, 3660.0, 5460.0, 7260.0]
_GAP_LABELS = ["1-6m", "6-21m", "21-41m", "41-61m", "61-91m", "91-121m", ">2h01"]
_MIN_CHANCE_CALLS = 10  # measurements in a pause bucket before the box shows lost/calls
_MIN_TABLE_CALLS = 5  # measurements before the summary shows the cold-cache table
_MIN_SUGGEST_CALLS = 20  # measurements of a model before the table suggests a pause for it
_SUGGEST_LOSS = 0.10  # suggested pause: the longest one where at most this share of calls lost the cache


def _measured_gap(row: Dict[str, Any]) -> Optional[float]:
    """The pause of a call the table counts, None for a call it leaves out."""
    gap = row.get("gap_s")
    if not _finite_number(gap) or gap < 60 or _is_auxiliary(row) or not row["evaluable"]:
        return None
    return float(gap)


def _cold_cells(rows: List[Dict[str, Any]], evaluated: List[Dict[str, Any]],
                model_filter: str = "") -> Dict[str, List[Dict[str, int]]]:
    """Calls and cold calls per model and pause bucket."""
    models: Dict[str, List[Dict[str, int]]] = {}
    for row in evaluated:
        model = str(row.get("model") or "unknown")
        if model_filter and model_filter not in model.lower():
            continue
        cells = models.setdefault(model, [{"calls": 0, "cold": 0} for _ in _GAP_LABELS])
        gap = _measured_gap(row)
        if gap is None:
            continue
        cell = cells[_bucket_index(gap, _GAP_EDGES)]
        cell["calls"] += 1
        cell["cold"] += int(bool(row["cold"]))
    return models


def _loss_blocks(points: List[Tuple[float, bool]]) -> List[Tuple[float, float, float, int]]:
    """Isotonic fit (pool adjacent violators) of 'lost' on the pause: the loss share never falls
    as the pause grows. Blocks of (first pause, last pause, loss share, calls)."""
    by_gap: Dict[float, List[int]] = {}
    for gap, lost in points:
        count = by_gap.setdefault(gap, [0, 0])
        count[0] += int(lost)
        count[1] += 1
    blocks: List[List[Any]] = []  # [first, last, lost, calls]
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


def _suggested_pause(points: List[Tuple[float, bool]]) -> str:
    """The longest pause whose fitted loss share is _SUGGEST_LOSS or less; none when even the
    shortest is above (a + after the pause: no block above, the longest pause seen);
    — below _MIN_SUGGEST_CALLS measurements."""
    if len(points) < _MIN_SUGGEST_CALLS:
        return "—"
    best = None
    for _, last, share, _ in _loss_blocks(points):
        if share > _SUGGEST_LOSS:
            return "none" if best is None else _short_pause(best)
        best = last
    return _short_pause(best) + "+"  # no block above the limit: nothing lost beyond the longest pause seen


def _suggested_pauses(evaluated: List[Dict[str, Any]], model_filter: str = "") -> Dict[str, str]:
    """The `suggested` column per model. A model that fails to compute gets —."""
    points: Dict[str, List[Tuple[float, bool]]] = {}
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


# --------------------------------------------------------------------------- #
# health check + graceful degradation
# --------------------------------------------------------------------------- #
_H: Dict[str, Any] = {
    "checks": {}, "noticed": set(),
    "config_errors": set(), "data_errors": set(),
    "pending_notices": [],  # one-off notices waiting to be posted to each session
}


def _set_data_errors(errors: set) -> None:
    _H["data_errors"] = errors
    for reason in errors:
        _notice(reason)


def _notice(reason: str) -> None:
    """ONE notice per process per reason, kept for transform_llm_output to post (once per session).
    `reason` is a plain sentence for the user."""
    if reason in _H["noticed"]:
        return
    _H["noticed"].add(reason)
    _H["pending_notices"].append(reason)


def _healthy(name: str) -> bool:
    """True unless the register-time check `name` failed (unknown => assume OK)."""
    return _H["checks"].get(name, (True, ""))[0]


def _module_file(module: str) -> Optional[Path]:
    """Source file of `module`, found without importing it or its parents."""
    import importlib.util

    parts = module.split(".")
    spec = importlib.util.find_spec(parts[0])  # top-level name only: imports nothing
    if spec is None:
        return None
    if len(parts) == 1:
        return Path(spec.origin) if spec.origin and spec.origin.endswith(".py") else None
    dirs = [Path(d) for d in spec.submodule_search_locations or []]
    for part in parts[1:-1]:
        dirs = [d / part for d in dirs]
    for d in dirs:
        for f in (d / (parts[-1] + ".py"), d / parts[-1] / "__init__.py"):
            if f.is_file():
                return f
    return None


def _binding(body: List[Any], name: str) -> Any:
    """The statement in `body` that defines `name` (def, class, assignment, import), else None."""
    import ast

    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(n, ast.Name) and n.id == name for t in targets for n in ast.walk(t)):
                return node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if any((a.asname or a.name.split(".")[0]) == name for a in node.names):
                return node
        elif isinstance(node, (ast.If, ast.Try)):
            inner = node.body + node.orelse + getattr(node, "finalbody", [])
            inner += [s for h in getattr(node, "handlers", []) for s in h.body]
            found = _binding(inner, name)
            if found is not None:
                return found
    return None


def _has_symbol(module: str, name: str) -> bool:
    """True if `module` really defines `name` ("Class.method" looks inside the class).

    No side effects: a loaded module is asked directly, else its source is parsed, never
    imported. A name left in a comment or a string does not count. Any error is False.
    """
    import ast

    try:
        mod = sys.modules.get(module)
        if mod is not None:
            obj: Any = mod
            for part in name.split("."):
                obj = getattr(obj, part)
            return True
        path = _module_file(module)
        if path is None:
            return False
        body = ast.parse(path.read_text(errors="ignore")).body
        *owners, last = name.split(".")
        for owner in owners:
            node = _binding(body, owner)
            if not isinstance(node, ast.ClassDef):
                return False
            body = node.body
        return _binding(body, last) is not None
    except Exception:
        return False


# How the plugin calls each Hermes function, read from the installed Hermes (tested_hermes).
# (module, name): (sync or async, positional names in order, names passed by keyword).
# Only what the plugin passes is listed: a new optional argument with a default is harmless,
# a new required argument, a renamed or moved one, a sync/async switch or a class is not.
# Symbols not listed here (constants, calls Hermes makes itself) are only checked to exist.
_CALLS: Dict[Tuple[str, str], Tuple[str, Tuple[str, ...], Tuple[str, ...]]] = {
    ("tools.terminal_tool", "_get_approval_callback"): ("sync", (), ()),
    ("tools.approval", "_gateway_notify_cb"): ("sync", ("session_key",), ()),
    ("tools.approval_gateway_wait", "_await_gateway_decision"):
        ("sync", ("session_key", "notify_cb", "approval_data"), ()),
    ("hermes_cli.cli_modal_mixin", "CLIModalMixin._normalize_slash_confirm_choice"):
        ("sync", ("self", "raw", "choices"), ()),
    ("agent.usage_pricing", "normalize_usage"): ("sync", ("response_usage",), ("provider", "api_mode")),
    ("tools.approval_context", "get_current_session_key"): ("sync", (), ("default",)),
    ("tools.skill_provenance", "get_current_write_origin"): ("sync", (), ()),
    ("hermes_cli.config", "read_raw_config"): ("sync", (), ()),
    ("hermes_cli.config", "read_raw_config_readonly"): ("sync", (), ()),  # newer Hermes only
    ("hermes_constants", "get_hermes_home"): ("sync", (), ()),
    ("gateway.session_context", "get_session_env"): ("sync", ("name", "default"), ()),
    ("tools.bot_mode_probe", "is_bot_mode_managed"): ("sync", ("home",), ()),
    ("tools.approval_context", "_is_single_query_approval_context"): ("sync", (), ()),
    ("tools.approval_context", "_get_approval_timeout"): ("sync", (), ()),
}
_OPTIONAL = {("hermes_cli.config", "read_raw_config_readonly")}


def _describe(module: str, name: str) -> Optional[Tuple[str, Optional[List[Tuple[str, str, bool]]]]]:
    """(what it is, its parameters) for `name`, or None when it cannot be told.

    what: "sync", "async", "class" or "variable". Parameters are (name, kind, has_default) with kind
    posonly, pos, var_pos, kwonly or var_kw. A loaded module is asked directly (inspect), else the
    source is parsed, never imported. An import or an alias we cannot judge gives None.
    """
    import ast
    import inspect

    mod = sys.modules.get(module)
    if mod is not None:
        obj: Any = mod
        for part in name.split("."):
            obj = getattr(obj, part)
        if inspect.isclass(obj):
            return "class", None
        if not callable(obj):
            return "variable", None
        what = "async" if inspect.iscoroutinefunction(obj) else "sync"
        try:
            kinds = {inspect.Parameter.POSITIONAL_ONLY: "posonly", inspect.Parameter.POSITIONAL_OR_KEYWORD: "pos",
                     inspect.Parameter.VAR_POSITIONAL: "var_pos", inspect.Parameter.KEYWORD_ONLY: "kwonly",
                     inspect.Parameter.VAR_KEYWORD: "var_kw"}
            params = [(p.name, kinds[p.kind], p.default is not inspect.Parameter.empty)
                      for p in inspect.signature(obj).parameters.values()]
        except (TypeError, ValueError):
            return what, None
        return what, params
    try:
        path = _module_file(module)
    except Exception:
        path = None  # cannot even find it: the existence check already says so
    if path is None:
        return None
    body = ast.parse(path.read_text(errors="ignore")).body
    *owners, last = name.split(".")
    for owner in owners:
        node = _binding(body, owner)
        if not isinstance(node, ast.ClassDef):
            return None
        body = node.body
    node = _binding(body, last)
    if isinstance(node, ast.ClassDef):
        return "class", None
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        literal = (ast.Constant, ast.List, ast.Dict, ast.Tuple, ast.Set)
        return ("variable", None) if isinstance(node.value, literal) else None
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    a = node.args
    positional = a.posonlyargs + a.args
    firsts = len(positional) - len(a.defaults)
    params = [(x.arg, "posonly" if i < len(a.posonlyargs) else "pos", i >= firsts) for i, x in enumerate(positional)]
    if a.vararg:
        params.append((a.vararg.arg, "var_pos", False))
    params += [(x.arg, "kwonly", d is not None) for x, d in zip(a.kwonlyargs, a.kw_defaults)]
    if a.kwarg:
        params.append((a.kwarg.arg, "var_kw", False))
    return ("async" if isinstance(node, ast.AsyncFunctionDef) else "sync"), params


def _call_problem(module: str, name: str) -> str:
    """"" when `name` still fits the way the plugin calls it (_CALLS), else a short plain reason.

    Report only: the caller shows it in the health text. An error reading Hermes' code is
    reported too, never raised.
    """
    short = name.split(".")[-1]
    spec = _CALLS.get((module, name))
    if spec is None:
        return ""
    try:
        found = _describe(module, name)
    except Exception as exc:
        return f"cannot read `{short}`: {type(exc).__name__}"
    if found is None:
        return ""
    want, pos, kw = spec
    what, params = found
    if what in ("class", "variable"):
        return f"Hermes changed `{short}`: it is no longer a function"
    if what != want:
        return f"Hermes changed `{short}`: it is " + ("now async" if what == "async" else "no longer async")
    if params is None:
        return ""
    positional = [p[0] for p in params if p[1] in ("posonly", "pos")]
    var_pos = any(p[1] == "var_pos" for p in params)
    var_kw = any(p[1] == "var_kw" for p in params)
    if len(pos) > len(positional) and not var_pos:
        return f"Hermes changed `{short}`: it no longer takes `{pos[len(positional)]}`"
    if positional[:len(pos)] != list(pos[:len(positional)]):
        return (f"Hermes changed `{short}`: its arguments are now ({', '.join(positional[:len(pos)])}), "
                f"not ({', '.join(pos)})")
    for k in kw:
        if not var_kw and not any(p[0] == k and p[1] in ("pos", "kwonly") for p in params):
            return f"Hermes changed `{short}`: it no longer takes `{k}`"
    for pname, kind, has_default in params:
        if kind in ("posonly", "pos", "kwonly") and not has_default and pname not in pos and pname not in kw:
            return f"Hermes changed `{short}`: it now needs `{pname}`"
    return ""


def _tested_version() -> str:
    try:
        import re

        m = re.search(r'^tested_hermes:\s*"?([0-9][^"\s]*)', (Path(__file__).parent / "plugin.yaml").read_text(), re.M)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _health() -> Dict[str, Tuple[bool, str]]:
    """Cheap, offline checks of every Hermes surface this plugin leans on."""
    out: Dict[str, Tuple[bool, str]] = {}

    def check(name: str, fn: Any) -> None:
        try:
            result = fn()
            if isinstance(result, str):  # a reason, already plain
                out[name] = (False, result)
            else:
                out[name] = (True, "") if result else (False, "missing or changed")
        except Exception as exc:
            out[name] = (False, f"{type(exc).__name__}: {exc}")

    def has(*symbols: Tuple[str, str]) -> Any:
        """True if every symbol exists and is still called the way we call it; False or a reason if not."""
        for module, symbol in symbols:
            if not _has_symbol(module, symbol):
                if (module, symbol) in _OPTIONAL:
                    continue
                return False
            problem = _call_problem(module, symbol)
            if problem:
                return problem
        return True

    check("approval_cb", lambda: has(("tools.terminal_tool", "_get_approval_callback")))
    # Only module-level symbols: the CLI's _prompt_text_input_modal, _approval_lock and _app live on
    # the running CLI object, which does not exist at register time. They fail open at call time.
    check("gateway_approval", lambda: has(("tools.approval", "_gateway_notify_cb"),
                                          ("tools.approval_gateway_wait", "_await_gateway_decision")))
    check("confirm_choice", lambda: has(("hermes_cli.cli_modal_mixin", "CLIModalMixin._normalize_slash_confirm_choice")))
    check("usage_api", lambda: has(("agent.usage_pricing", "normalize_usage")))
    check("session_key", lambda: has(("tools.approval_context", "get_current_session_key")))
    check("write_origin", lambda: has(("tools.skill_provenance", "get_current_write_origin")))
    check("request_middleware", lambda: has(("hermes_cli.middleware", "apply_llm_request_middleware")))
    check("config_api", lambda: has(("hermes_cli.config", "read_raw_config"),
                                    ("hermes_cli.config", "read_raw_config_readonly"),
                                    ("hermes_constants", "get_hermes_home")))
    check("session_env", lambda: has(("gateway.session_context", "get_session_env")))
    check("bot_chat", lambda: has(("tools.bot_mode_probe", "BOT_CHAT_TITLE"),
                                  ("tools.bot_mode_probe", "is_bot_mode_managed")))
    check("single_query", lambda: has(("tools.approval_context", "_is_single_query_approval_context")))
    check("approval_timeout", lambda: has(("tools.approval_context", "_get_approval_timeout")))
    check("post_message", lambda: callable(getattr(_CTX, "post_message", None)))

    def db(need: Dict[str, Any]) -> bool:
        import sqlite3

        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
        if not (home / "state.db").exists():
            return True  # fresh Hermes home: Hermes creates state.db later, nothing to verify yet
        con = sqlite3.connect(_state_db_uri(home), uri=True, timeout=1.0)
        try:
            for table, cols in need.items():
                have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
                if not cols <= have:
                    raise RuntimeError(f"state.db {table} lacks {sorted(cols - have) or 'table'}")
        finally:
            con.close()
        return True

    check("state_db", lambda: db({"messages": {"session_id", "role", "content", "timestamp"}}))
    # Own check: only the Bot Chat title lookup and the chat-app export hint read `sessions`.
    check("sessions_db", lambda: db({"sessions": {"id", "title", "session_key", "started_at"}}))

    # Information only: a newer Hermes is not a fault. The checks above catch real breakage.
    try:
        import importlib.metadata

        cur = importlib.metadata.version("hermes-agent")
    except Exception:
        cur = "?"
    out["hermes_version"] = (True, f"{cur} (tested {_tested_version() or '?'})")
    return out


# One plain sentence per check, printed in the chat. The technical name and detail stay in
# `/cacheguard` (health section) only.
_NO_BOX_TEXT = "cacheguard cannot show the box here: it only warns."
_HEALTH_EFFECTS = {
    "approval_cb": _NO_BOX_TEXT,
    "gateway_approval": _NO_BOX_TEXT,
    "state_db": "cacheguard cannot read the chat history: some features are off.",
    "confirm_choice": "typed answers in the cacheguard box may not work: use the buttons.",
    "usage_api": "cacheguard cannot read cache numbers: results may be missing.",
    "session_key": _NO_BOX_TEXT,
    "request_middleware": "cacheguard lines may be sent to the model.",
    "llm_request": "cacheguard lines may reach the model.",
    "transform_llm_output": "no result lines.",
    "write_origin": "cacheguard may count background calls as yours.",
    "session_env": "cacheguard may treat cron and kanban runs as chats.",
    "sessions_db": "Bot Chat and export in the gateway may not find this chat.",
    "bot_chat": "cacheguard may treat Bot Chat as a normal chat.",
    "single_query": "with -Q and -z, cacheguard may ask where it should only warn.",
    "approval_timeout": "the box timeout may differ from Hermes.",
    "post_message": "cacheguard cannot show its lines in this Hermes.",
    "middleware": "cacheguard is off: it could not start.",
}


def _run_health() -> None:
    _H["checks"] = _health()
    for name, effect in _HEALTH_EFFECTS.items():
        if not _healthy(name):
            _notice(effect)


def _health_text() -> str:
    bad = [f"{k}: {d}" + (f" ({_HEALTH_EFFECTS[k]})" if k in _HEALTH_EFFECTS else "")
           for k, (ok, d) in _H["checks"].items() if not ok]
    bad.extend(f"config: {reason}" for reason in sorted(_H.get("config_errors", set())))
    bad.extend(f"data: {reason}" for reason in sorted(_H.get("data_errors", set())))
    if _H.get("box_failed"):
        bad.append("The cacheguard box failed this session, so it is off until Hermes restarts: "
                   f"block acts as warn. ({_H['box_failed']})")
    if not bad:
        return ""
    return "\n".join(["health: degraded"] + [f"  - {b}" for b in bad])


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
_CLI_HELP = "show or set up cacheguard from the terminal (no Hermes chat needed)"


def _cli_setup_parser(parser) -> None:
    """`hermes cacheguard [setup|set|help]`: arguments only; validation stays in the chat functions."""
    actions = parser.add_subparsers(dest="cache_guard_action", metavar="<action>")
    setup = actions.add_parser("setup", help="set the mode of cli, desktop, gateway or all")
    setup.add_argument("group", help="all | cli | desktop | gateway")
    setup.add_argument("mode", help="off | warn | block")
    setup.add_argument("minutes", nargs="?", metavar="pause_m", help="optional: pause in minutes before the guard acts")
    setup.add_argument("tokens", nargs="?", metavar="size_k", help="optional: size in k tokens (give both or neither)")
    change = actions.add_parser("set", help="change one setting, e.g. set pause_m 4")
    change.add_argument("words", nargs="*", metavar="<name> <value>", help="name and value, as in /cacheguard set")
    actions.add_parser("help", help="list the commands")


_CLI_HELP_NOTE = ("\n\nIn the terminal, `hermes cacheguard` takes: (nothing) | help | setup | set.\n"
                  "The other commands work inside a Hermes chat.")


def _cli_handler(args) -> int:
    """Print the same text as the in-chat command. Exit code 0 when it worked."""
    action = getattr(args, "cache_guard_action", None)
    if action is None:  # the summary
        print(_cmd_show("", terminal=True))
        return 0
    if action == "help":
        print(_HELP_TEXT + _CLI_HELP_NOTE)
        return 0
    if action == "setup":
        extra = [v for v in (args.minutes, args.tokens) if v is not None]
        text = _cmd_setup([args.group, args.mode, *extra])
    else:  # set
        text = _cmd_set(args.words)
    ok = "saved to config" in text
    print(text.replace("/cacheguard ", "hermes cacheguard "))  # usage lines name the terminal command
    return 0 if ok else 1


def _register_cli(ctx: Any) -> None:
    if not hasattr(ctx, "register_cli_command"):  # older Hermes: no terminal command, plugin still loads
        return
    try:
        ctx.register_cli_command(
            name="cacheguard", help=_CLI_HELP, setup_fn=_cli_setup_parser, handler_fn=_cli_handler,
            description="Show or set cacheguard without opening Hermes. Example: hermes -p NAME cacheguard setup all block 10 20",
        )
    except Exception:
        pass


def register(ctx: Any) -> None:
    global _CTX
    _CTX = ctx
    _register_cli(ctx)
    try:
        _run_health()
    except Exception:
        pass
    try:
        ctx.register_middleware("llm_execution", _on_llm_execution)
    except Exception as exc:
        _H["checks"]["middleware"] = (False, f"llm_execution: {type(exc).__name__}: {exc}")
        _notice(_HEALTH_EFFECTS["middleware"])
    try:
        ctx.register_middleware("llm_request", _on_llm_request)
    except Exception as exc:
        _H["checks"]["llm_request"] = (False, f"{type(exc).__name__}: {exc}")
    try:
        ctx.register_hook("transform_llm_output", _transform_llm_output)
    except Exception as exc:
        _H["checks"]["transform_llm_output"] = (False, f"{type(exc).__name__}: {exc}")
    try:
        ctx.register_command(
            "cacheguard",
            _cmd_entry,
            description="Show cacheguard status. 'setup' and 'set' change settings; 'reset' clears saved history.",
            args_hint="[help | all | <model> | setup <all|cli|desktop|gateway> <mode> [pause_m] [size_k] | set <name> <value> | export <first> <last> | export all | reset]",
        )
    except Exception:
        pass
