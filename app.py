from __future__ import annotations

import os
import csv
import io
import threading
import time
import uuid
import json
import secrets
from dataclasses import asdict
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, render_template, request, session

from archiver import ArchiveWebTool
from db import SQLiteStore


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
DB_PATH = BASE_DIR / "archive_cache.sqlite3"
OUTPUT_ROOT_DIR = Path(os.environ.get("OUTPUT_ROOT_DIR", str(OUTPUT_DIR))).expanduser().resolve()
OUTPUT_ROOT_DIR.mkdir(parents=True, exist_ok=True)
ALLOW_UNSAFE_OUTPUT_ROOT = os.environ.get("ALLOW_UNSAFE_OUTPUT_ROOT", "0").strip().lower() in {"1", "true", "yes", "on"}

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
tool = ArchiveWebTool(timeout=60)
store = SQLiteStore(DB_PATH)
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
MISSING_JOBS: dict[str, dict] = {}
MISSING_JOBS_LOCK = threading.Lock()
INSPECT_JOBS: dict[str, dict] = {}
INSPECT_JOBS_LOCK = threading.Lock()
ANALYZE_JOBS: dict[str, dict] = {}
ANALYZE_JOBS_LOCK = threading.Lock()
ANALYZE_BATCH_JOBS: dict[str, dict] = {}
ANALYZE_BATCH_JOBS_LOCK = threading.Lock()
CHECK_JOBS: dict[str, dict] = {}
CHECK_JOBS_LOCK = threading.Lock()
SITEMAP_JOBS: dict[str, dict] = {}
SITEMAP_JOBS_LOCK = threading.Lock()

CACHE_TTL_SECONDS = 900
INSPECT_CACHE: dict[str, tuple[float, dict]] = {}
ANALYSIS_CACHE: dict[str, tuple[float, dict]] = {}
ANALYZE_DEEP_CDX_LIMIT = 12000
PERSISTENT_CACHE_MAX_AGE_SECONDS = 315360000
MAX_ACTIVE_JOBS = int(os.environ.get("MAX_ACTIVE_JOBS", "4"))
REQUIRE_LOCAL_MUTATIONS = (os.environ.get("REQUIRE_LOCAL_MUTATIONS", "1").strip().lower() in {"1", "true", "yes", "on"})
APP_API_TOKEN = (os.environ.get("APP_API_TOKEN") or "").strip()
ACTIVE_JOBS_LOCK = threading.Lock()
ACTIVE_JOBS_COUNT = 0
JOB_RETENTION_SECONDS = int(os.environ.get("JOB_RETENTION_SECONDS", "3600"))
JOB_CLEANUP_INTERVAL_SECONDS = int(os.environ.get("JOB_CLEANUP_INTERVAL_SECONDS", "60"))
_LAST_JOB_CLEANUP_TS = 0.0
DB_PRUNE_INTERVAL_SECONDS = int(os.environ.get("DB_PRUNE_INTERVAL_SECONDS", "600"))
DB_CACHE_RETENTION_SECONDS = int(os.environ.get("DB_CACHE_RETENTION_SECONDS", str(14 * 24 * 3600)))
DB_JOBS_RETENTION_SECONDS = int(os.environ.get("DB_JOBS_RETENTION_SECONDS", str(30 * 24 * 3600)))
_LAST_DB_PRUNE_TS = 0.0


class JobCapacityError(RuntimeError):
    pass


def _with_cache_meta(payload: dict, source: str, age_seconds: int) -> dict:
    out = dict(payload or {})
    out["_cache"] = {
        "source": source,
        "age_seconds": max(0, int(age_seconds)),
    }
    return out


def _cache_get(cache: dict[str, tuple[float, dict]], key: str) -> Optional[dict]:
    item = cache.get(key)
    if not item:
        return None
    ts, data = item
    if (time.time() - ts) > CACHE_TTL_SECONDS:
        cache.pop(key, None)
        return None
    return data


def _cache_set(cache: dict[str, tuple[float, dict]], key: str, data: dict) -> dict:
    cache[key] = (time.time(), data)
    return data


def _purge_memory_cache_for_target(target_url: str) -> None:
    target_url = _normalize_target_url(target_url)
    target_host = urlparse(target_url).netloc

    def _same_host(key_target: str) -> bool:
        if not key_target:
            return False
        key_host = urlparse(_normalize_target_url(key_target)).netloc
        return key_host == target_host

    for key in list(INSPECT_CACHE.keys()):
        parts = key.split("|", 3)
        if len(parts) >= 2 and _same_host(parts[1]):
            INSPECT_CACHE.pop(key, None)
    for key in list(ANALYSIS_CACHE.keys()):
        parts = key.split("|", 4)
        if len(parts) >= 2 and _same_host(parts[1]):
            ANALYSIS_CACHE.pop(key, None)


def _normalize_target_url(target_url: str) -> str:
    raw = (target_url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.netloc or parsed.path or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return raw
    return f"https://{host}"


def _get_csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return str(token)


def _is_local_request() -> bool:
    addr = (request.remote_addr or "").strip()
    return addr in {"127.0.0.1", "::1", "localhost"}


def _security_error(message: str, status: int = 403):
    if request.path.startswith("/api/") or request.path.endswith("/start") or request.path.endswith("/pause") or request.path.endswith("/resume") or request.path.endswith("/stop") or request.is_json:
        return jsonify({"ok": False, "error": message}), status
    return render_template("index.html", error=message, result=None, inspect=None, analysis=None, check=None), status


def _claim_job_slot() -> None:
    global ACTIVE_JOBS_COUNT
    with ACTIVE_JOBS_LOCK:
        if ACTIVE_JOBS_COUNT >= max(1, MAX_ACTIVE_JOBS):
            raise JobCapacityError(f"Too many active jobs ({ACTIVE_JOBS_COUNT}/{MAX_ACTIVE_JOBS}). Wait for current jobs to finish.")
        ACTIVE_JOBS_COUNT += 1


def _release_job_slot() -> None:
    global ACTIVE_JOBS_COUNT
    with ACTIVE_JOBS_LOCK:
        ACTIVE_JOBS_COUNT = max(0, ACTIVE_JOBS_COUNT - 1)


def _cleanup_old_jobs() -> None:
    now = time.time()

    def _cleanup_map(job_map: dict[str, dict], lock: threading.Lock) -> None:
        with lock:
            for job_id, job in list(job_map.items()):
                state = str(job.get("state") or "")
                if state not in {"done", "error", "stopped", "cancelled"}:
                    continue
                started_at = float(job.get("started_at") or now)
                if (now - started_at) > max(60, JOB_RETENTION_SECONDS):
                    job_map.pop(job_id, None)

    _cleanup_map(JOBS, JOBS_LOCK)
    _cleanup_map(MISSING_JOBS, MISSING_JOBS_LOCK)
    _cleanup_map(INSPECT_JOBS, INSPECT_JOBS_LOCK)
    _cleanup_map(ANALYZE_JOBS, ANALYZE_JOBS_LOCK)
    _cleanup_map(ANALYZE_BATCH_JOBS, ANALYZE_BATCH_JOBS_LOCK)
    _cleanup_map(CHECK_JOBS, CHECK_JOBS_LOCK)
    _cleanup_map(SITEMAP_JOBS, SITEMAP_JOBS_LOCK)


def _maybe_cleanup_jobs() -> None:
    global _LAST_JOB_CLEANUP_TS
    now = time.time()
    if (now - _LAST_JOB_CLEANUP_TS) < max(5, JOB_CLEANUP_INTERVAL_SECONDS):
        return
    _cleanup_old_jobs()
    _LAST_JOB_CLEANUP_TS = now


def _maybe_prune_db() -> None:
    global _LAST_DB_PRUNE_TS
    now = time.time()
    if (now - _LAST_DB_PRUNE_TS) < max(30, DB_PRUNE_INTERVAL_SECONDS):
        return
    try:
        store.prune_old_data(DB_CACHE_RETENTION_SECONDS, DB_JOBS_RETENTION_SECONDS)
    except Exception:
        pass
    _LAST_DB_PRUNE_TS = now


@app.before_request
def apply_security_guards():
    _maybe_cleanup_jobs()
    _maybe_prune_db()
    if app.config.get("TESTING"):
        return None
    _get_csrf_token()

    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None

    if REQUIRE_LOCAL_MUTATIONS and not _is_local_request():
        return _security_error("This app allows write operations from localhost only.", 403)

    if APP_API_TOKEN and not _is_local_request():
        sent_token = (
            request.headers.get("X-App-Token")
            or request.form.get("app_token")
            or (request.get_json(silent=True) or {}).get("app_token")
            or ""
        ).strip()
        if sent_token != APP_API_TOKEN:
            return _security_error("Invalid app token.", 401)

    expected = str(session.get("csrf_token") or "")
    provided = (
        request.headers.get("X-CSRF-Token")
        or request.form.get("csrf_token")
        or (request.get_json(silent=True) or {}).get("csrf_token")
        or ""
    )
    if not expected or not provided or provided != expected:
        return _security_error("Invalid CSRF token.", 403)
    return None


@app.context_processor
def inject_security_tokens():
    return {
        "csrf_token": _get_csrf_token(),
    }


def _elapsed_seconds(started_at: float) -> int:
    return max(0, int(time.time() - float(started_at or time.time())))


def _normalize_progress(
    payload: Optional[dict],
    started_at: float,
    *,
    stage: str = "running",
    message: str = "Working",
    current_item: str = "",
) -> dict:
    raw = dict(payload or {})
    pct = raw.get("percent", 0)
    try:
        percent = int(float(pct))
    except (TypeError, ValueError):
        percent = 0
    percent = max(0, min(100, percent))

    current = raw.get("current_item") or current_item
    if not current:
        for key in ("current_url", "current_variant", "current_snapshot", "label", "snapshot"):
            value = raw.get(key)
            if value:
                current = str(value)
                break

    raw["stage"] = str(raw.get("stage") or stage)
    raw["message"] = str(raw.get("message") or message)
    raw["percent"] = percent
    raw["current_item"] = str(current or "")
    raw["elapsed_seconds"] = _elapsed_seconds(started_at)
    return raw


def _queued_progress(started_at: float, **extra: object) -> dict:
    base = {
        "stage": "queued",
        "message": "Job queued",
        "percent": 0,
        "current_item": "",
    }
    base.update(extra)
    return _normalize_progress(base, started_at, stage="queued", message="Job queued")


def _cached_inspect(target_url: str, display_limit: int, cdx_limit: int) -> dict:
    target_url = _normalize_target_url(target_url)
    key = f"i|{target_url}|{display_limit}|{cdx_limit}"
    item = INSPECT_CACHE.get(key)
    if item is not None:
        ts, data = item
        age = int(time.time() - ts)
        if age <= CACHE_TTL_SECONDS:
            return _with_cache_meta(data, "memory", age)
        INSPECT_CACHE.pop(key, None)
    got_db = store.get_inspect_cache_with_meta(key, CACHE_TTL_SECONDS)
    if got_db is not None:
        payload = got_db["payload"]
        _cache_set(INSPECT_CACHE, key, payload)
        return _with_cache_meta(payload, "sqlite", int(got_db.get("age_seconds", 0)))
    data = tool.inspect(target_url, display_limit=display_limit, cdx_limit=cdx_limit)
    store.set_inspect_cache(key, target_url, display_limit, cdx_limit, data)
    store.upsert_project(target_url)
    _cache_set(INSPECT_CACHE, key, data)
    return _with_cache_meta(data, "archive", 0)


def _cached_inspect_only(target_url: str, display_limit: int, cdx_limit: int) -> Optional[dict]:
    target_url = _normalize_target_url(target_url)
    key = f"i|{target_url}|{display_limit}|{cdx_limit}"
    item = INSPECT_CACHE.get(key)
    if item is not None:
        ts, data = item
        age = int(time.time() - ts)
        if age <= CACHE_TTL_SECONDS:
            return _with_cache_meta(data, "memory", age)
        INSPECT_CACHE.pop(key, None)
    got_db = store.get_inspect_cache_with_meta(key, CACHE_TTL_SECONDS)
    if got_db is not None:
        payload = got_db["payload"]
        _cache_set(INSPECT_CACHE, key, payload)
        return _with_cache_meta(payload, "sqlite", int(got_db.get("age_seconds", 0)))
    return None


def _cached_analyze(target_url: str, selected_snapshot: str, cdx_limit: int = 12000) -> dict:
    target_url = _normalize_target_url(target_url)
    key = f"a|{target_url}|{selected_snapshot}|{cdx_limit}"
    item = ANALYSIS_CACHE.get(key)
    if item is not None:
        ts, data = item
        age = int(time.time() - ts)
        if age <= CACHE_TTL_SECONDS:
            return _with_cache_meta(data, "memory", age)
        ANALYSIS_CACHE.pop(key, None)
    got_db = store.get_analyze_cache_with_meta(key, CACHE_TTL_SECONDS)
    if got_db is not None:
        payload = got_db["payload"]
        _cache_set(ANALYSIS_CACHE, key, payload)
        return _with_cache_meta(payload, "sqlite", int(got_db.get("age_seconds", 0)))
    data = tool.analyze(target_url, selected_snapshot, cdx_limit=cdx_limit)
    store.set_analyze_cache(key, target_url, data.get("selected_snapshot", selected_snapshot), cdx_limit, data)
    store.upsert_project(
        target_url,
        snapshot=data.get("selected_snapshot"),
        site_type=data.get("site_type"),
        estimated_files=int(data.get("estimated_files", 0) or 0),
        estimated_size=int(data.get("estimated_size_bytes", 0) or 0),
    )
    _cache_set(ANALYSIS_CACHE, key, data)
    return _with_cache_meta(data, "archive", 0)


def _best_inspect_for_render(target_url: str, display_limit: Optional[int] = None, cdx_limit: Optional[int] = None) -> Optional[dict]:
    target_url = _normalize_target_url(target_url)
    if not target_url:
        return None

    if display_limit is not None and cdx_limit is not None:
        try:
            return _cached_inspect(target_url, int(display_limit), int(cdx_limit))
        except Exception:
            pass

    now = time.time()
    best_ts = 0.0
    best_payload: Optional[dict] = None
    prefix = f"i|{target_url}|"
    for key, item in INSPECT_CACHE.items():
        if not key.startswith(prefix):
            continue
        ts, payload = item
        if (now - ts) > CACHE_TTL_SECONDS:
            continue
        if ts > best_ts:
            best_ts = ts
            best_payload = payload
    if best_payload is not None:
        age = int(now - best_ts)
        return _with_cache_meta(best_payload, "memory", age)

    latest = store.get_latest_inspect_for_url(target_url, CACHE_TTL_SECONDS)
    if latest is not None:
        payload = latest["payload"]
        cache_key = str(latest.get("cache_key", ""))
        if cache_key:
            _cache_set(INSPECT_CACHE, cache_key, payload)
        return _with_cache_meta(payload, "sqlite", int(latest.get("age_seconds", 0)))
    return None


def _best_analyze_for_render(target_url: str, selected_snapshot: str = "", cdx_limit: Optional[int] = None) -> Optional[dict]:
    target_url = _normalize_target_url(target_url)
    if not target_url:
        return None

    if selected_snapshot and cdx_limit is not None:
        try:
            return _cached_analyze(target_url, selected_snapshot, cdx_limit=int(cdx_limit))
        except Exception:
            pass

    now = time.time()
    best_ts = 0.0
    best_payload: Optional[dict] = None
    prefix = f"a|{target_url}|"
    for key, item in ANALYSIS_CACHE.items():
        if not key.startswith(prefix):
            continue
        if selected_snapshot and f"|{selected_snapshot}|" not in key:
            continue
        ts, payload = item
        if (now - ts) > CACHE_TTL_SECONDS:
            continue
        if ts > best_ts:
            best_ts = ts
            best_payload = payload
    if best_payload is not None:
        age = int(now - best_ts)
        return _with_cache_meta(best_payload, "memory", age)

    latest = store.get_latest_analyze_for_url(
        target_url,
        CACHE_TTL_SECONDS,
        snapshot=selected_snapshot or None,
    )
    if latest is not None:
        payload = latest["payload"]
        cache_key = str(latest.get("cache_key", ""))
        if cache_key:
            _cache_set(ANALYSIS_CACHE, cache_key, payload)
        return _with_cache_meta(payload, "sqlite", int(latest.get("age_seconds", 0)))
    return None


def _list_output_choices(current: str = "") -> list[str]:
    choices: set[str] = {str(OUTPUT_ROOT_DIR)}
    if current:
        try:
            choices.add(str(_resolve_output_root(current)))
        except Exception:
            choices.add(str(OUTPUT_ROOT_DIR))

    try:
        for manifest in OUTPUT_ROOT_DIR.glob("**/manifest.json"):
            choices.add(str(manifest.parent))
            choices.add(str(manifest.parent.parent))
    except Exception:
        pass

    return sorted(choices)


@app.context_processor
def inject_template_defaults():
    current = ""
    try:
        current = request.form.get("output_root", "").strip()
    except Exception:
        current = ""
    return {
        "output_choices": _list_output_choices(current),
        "recent_projects": store.list_recent_projects(limit=8),
        "recent_jobs": store.list_recent_jobs(limit=10),
    }


@app.get("/")
def index():
    return render_template(
        "index.html",
        result=None,
        inspect=None,
        analysis=None,
        check=None,
        selected_snapshot=None,
        target_url="",
        output_root=str(OUTPUT_ROOT_DIR),
        error=None,
    )


@app.post("/recent-projects/delete")
def delete_recent_project():
    payload = request.get_json(silent=True) or {}
    target_url = str(payload.get("target_url") or request.form.get("target_url") or "").strip()
    purge_related = _parse_bool(str(payload.get("purge_related") or request.form.get("purge_related") or "1"), default=True)
    if not target_url:
        return jsonify({"ok": False, "error": "target_url is required"}), 400

    removed = store.delete_project(target_url, purge_related=purge_related)
    _purge_memory_cache_for_target(target_url)
    return jsonify({"ok": True, "target_url": target_url, "removed": removed})


@app.get("/project/data-status")
def project_data_status():
    target_url = request.args.get("target_url", "").strip()
    if not target_url:
        return jsonify({"ok": False, "error": "target_url is required"}), 400
    status = store.get_project_data_status(target_url)
    return jsonify({"ok": True, "status": status})


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                fp = Path(root) / name
                try:
                    total += int(fp.stat().st_size)
                except Exception:
                    pass
    except Exception:
        return 0
    return total


def _jobs_state_counts(job_map: dict[str, dict], lock: threading.Lock) -> dict[str, int]:
    counts: dict[str, int] = {}
    with lock:
        for job in job_map.values():
            state = str(job.get("state") or "unknown")
            counts[state] = counts.get(state, 0) + 1
    return counts


@app.get("/diagnostics")
def diagnostics():
    payload = {
        "ok": True,
        "config": {
            "host": os.environ.get("HOST", "127.0.0.1"),
            "port": int(os.environ.get("PORT", "5000")),
            "max_active_jobs": MAX_ACTIVE_JOBS,
            "allow_unsafe_output_root": ALLOW_UNSAFE_OUTPUT_ROOT,
            "output_root_dir": str(OUTPUT_ROOT_DIR),
            "require_local_mutations": REQUIRE_LOCAL_MUTATIONS,
        },
        "runtime": {
            "active_jobs": ACTIVE_JOBS_COUNT,
            "jobs": {
                "download": _jobs_state_counts(JOBS, JOBS_LOCK),
                "missing": _jobs_state_counts(MISSING_JOBS, MISSING_JOBS_LOCK),
                "inspect": _jobs_state_counts(INSPECT_JOBS, INSPECT_JOBS_LOCK),
                "analyze": _jobs_state_counts(ANALYZE_JOBS, ANALYZE_JOBS_LOCK),
                "analyze_batch": _jobs_state_counts(ANALYZE_BATCH_JOBS, ANALYZE_BATCH_JOBS_LOCK),
                "check": _jobs_state_counts(CHECK_JOBS, CHECK_JOBS_LOCK),
                "sitemap": _jobs_state_counts(SITEMAP_JOBS, SITEMAP_JOBS_LOCK),
            },
        },
        "storage": {
            "db_path": str(DB_PATH),
            "db_size_bytes": int(DB_PATH.stat().st_size) if DB_PATH.exists() else 0,
            "output_size_bytes": _dir_size_bytes(OUTPUT_ROOT_DIR),
        },
    }
    return jsonify(payload)


def _parse_max_files(value: Optional[str]) -> int:
    raw = (value or "400").strip()
    try:
        max_files = int(raw)
    except ValueError:
        max_files = 400
    return max(50, min(max_files, 5000))


def _parse_missing_limit(value: Optional[str]) -> int:
    raw = (value or "300").strip()
    try:
        amount = int(raw)
    except ValueError:
        amount = 300
    return max(1, min(amount, 5000))


def _parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _parse_int(value: Optional[str], default: int, min_value: int, max_value: int) -> int:
    raw = (value or "").strip()
    try:
        num = int(raw)
    except ValueError:
        num = default
    return max(min_value, min(num, max_value))


def _is_within(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _resolve_output_root(value: Optional[str]) -> Path:
    raw = (value or "").strip()
    if not raw:
        path = OUTPUT_ROOT_DIR
    else:
        candidate = Path(raw).expanduser()
        path = candidate if candidate.is_absolute() else (BASE_DIR / candidate)
    path = path.resolve()
    if not ALLOW_UNSAFE_OUTPUT_ROOT and not _is_within(OUTPUT_ROOT_DIR, path):
        raise ValueError(f"Output path must be inside {OUTPUT_ROOT_DIR}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _build_sitemap_from_analysis(analysis: dict) -> dict:
    pages = sorted(set(analysis.get("site_pages", [])))
    folders = analysis.get("top_folders", [])
    grouped: dict[str, list[str]] = {}

    for page in pages:
        clean = str(page)
        if not clean.startswith("/"):
            clean = "/" + clean
        parts = [p for p in clean.split("/") if p]
        key = "/" if not parts else f"/{parts[0]}/"
        grouped.setdefault(key, []).append(clean)

    groups = [
        {
            "folder": folder,
            "count": len(items),
            "pages": items[:50],
        }
        for folder, items in sorted(grouped.items(), key=lambda x: len(x[1]), reverse=True)
    ]

    return {
        "total_pages": len(pages),
        "pages": pages,
        "top_folders": folders,
        "groups": groups,
    }


@app.post("/inspect")
def inspect_target():
    target_url = _normalize_target_url(request.form.get("target_url", "").strip())
    output_root = request.form.get("output_root", str(OUTPUT_ROOT_DIR)).strip()
    display_limit = _parse_int(request.form.get("display_limit"), default=10, min_value=5, max_value=2000)
    cdx_limit = _parse_int(request.form.get("cdx_limit"), default=1500, min_value=500, max_value=100000)
    try:
        inspect = _cached_inspect(target_url, display_limit, cdx_limit)
    except Exception as exc:
        return render_template(
            "index.html",
            result=None,
            inspect=None,
            analysis=None,
            check=None,
            selected_snapshot=None,
            target_url=target_url,
            output_root=output_root,
            display_limit=display_limit,
            cdx_limit=cdx_limit,
            error=str(exc),
        )

    selected_snapshot = inspect["latest_ok_snapshot"] if inspect["snapshots"] else None
    return render_template(
        "index.html",
        result=None,
        inspect=inspect,
        analysis=None,
        check=None,
        selected_snapshot=selected_snapshot,
        target_url=target_url,
        output_root=output_root,
        display_limit=display_limit,
        cdx_limit=cdx_limit,
        error=None,
    )


@app.post("/inspect/start")
def inspect_start():
    target_url = _normalize_target_url(request.form.get("target_url", "").strip())
    output_root_input = request.form.get("output_root", str(OUTPUT_ROOT_DIR)).strip()
    display_limit = _parse_int(request.form.get("display_limit"), default=10, min_value=5, max_value=2000)
    cdx_limit = _parse_int(request.form.get("cdx_limit"), default=1500, min_value=500, max_value=100000)
    force_refresh = _parse_bool(request.form.get("force_refresh"), default=False)
    if not target_url:
        return jsonify({"ok": False, "error": "URL is required"}), 400

    if not force_refresh:
        cached = _cached_inspect_only(target_url, display_limit, cdx_limit)
        if cached is not None:
            cache_meta = cached.get("_cache", {}) if isinstance(cached, dict) else {}
            cache_source = str(cache_meta.get("source", "local"))
            cache_age = int(cache_meta.get("age_seconds", 0) or 0)
            job_id = uuid.uuid4().hex
            started_at = time.time()
            with INSPECT_JOBS_LOCK:
                INSPECT_JOBS[job_id] = {
                    "state": "done",
                    "paused": False,
                    "cancelled": False,
                    "error": None,
                    "started_at": started_at,
                    "target_url": target_url,
                    "output_root": output_root_input,
                    "display_limit": display_limit,
                    "cdx_limit": cdx_limit,
                    "progress": _normalize_progress({
                        "stage": "done",
                        "message": "Loaded from local cache",
                        "percent": 100,
                        "variants_done": 0,
                        "variants_total": 0,
                        "current_variant": "",
                        "current_item": "cache",
                        "total_captures": cached.get("total_snapshots", 0),
                        "total_ok": cached.get("total_ok_snapshots", 0),
                        "cache_source": cache_source,
                        "cache_age_seconds": cache_age,
                    }, started_at, stage="done", message="Loaded from local cache"),
                    "result": cached,
                }
            return jsonify({"ok": True, "job_id": job_id, "cached": True})

    try:
        job_id = _start_inspect_job(target_url, output_root_input, display_limit, cdx_limit)
    except JobCapacityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 429
    return jsonify({"ok": True, "job_id": job_id})


@app.get("/inspect/status/<job_id>")
def inspect_status(job_id: str):
    with INSPECT_JOBS_LOCK:
        job = INSPECT_JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found"}), 404
    return jsonify({"ok": True, **job})


@app.post("/inspect/pause/<job_id>")
def inspect_pause(job_id: str):
    with INSPECT_JOBS_LOCK:
        job = INSPECT_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["paused"] = True
        job["state"] = "paused"
    return jsonify({"ok": True})


@app.post("/inspect/resume/<job_id>")
def inspect_resume(job_id: str):
    with INSPECT_JOBS_LOCK:
        job = INSPECT_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["paused"] = False
        if job.get("state") == "paused":
            job["state"] = "running"
    return jsonify({"ok": True})


@app.post("/inspect/stop/<job_id>")
def inspect_stop(job_id: str):
    with INSPECT_JOBS_LOCK:
        job = INSPECT_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["cancelled"] = True
        job["paused"] = False
        job["state"] = "stopping"
    return jsonify({"ok": True})


@app.post("/analyze/start")
def analyze_start():
    target_url = _normalize_target_url(request.form.get("target_url", "").strip())
    selected_snapshot = request.form.get("selected_snapshot", "").strip()
    output_root = request.form.get("output_root", str(OUTPUT_ROOT_DIR)).strip()
    display_limit = _parse_int(request.form.get("display_limit"), default=10, min_value=5, max_value=2000)
    cdx_limit = _parse_int(request.form.get("cdx_limit"), default=ANALYZE_DEEP_CDX_LIMIT, min_value=500, max_value=100000)
    if not target_url:
        return jsonify({"ok": False, "error": "URL is required"}), 400
    try:
        job_id = _start_analyze_job(target_url, selected_snapshot, output_root, cdx_limit, display_limit)
    except JobCapacityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 429
    return jsonify({"ok": True, "job_id": job_id})


@app.get("/analyze/status/<job_id>")
def analyze_status(job_id: str):
    with ANALYZE_JOBS_LOCK:
        job = ANALYZE_JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found"}), 404
    return jsonify({"ok": True, **job})


@app.post("/analyze/pause/<job_id>")
def analyze_pause(job_id: str):
    with ANALYZE_JOBS_LOCK:
        job = ANALYZE_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["paused"] = True
        job["state"] = "paused"
    return jsonify({"ok": True})


@app.post("/analyze/resume/<job_id>")
def analyze_resume(job_id: str):
    with ANALYZE_JOBS_LOCK:
        job = ANALYZE_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["paused"] = False
        if job.get("state") == "paused":
            job["state"] = "running"
    return jsonify({"ok": True})


@app.post("/analyze/stop/<job_id>")
def analyze_stop(job_id: str):
    with ANALYZE_JOBS_LOCK:
        job = ANALYZE_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["cancelled"] = True
        job["paused"] = False
        job["state"] = "stopping"
    return jsonify({"ok": True})


@app.get("/analyze/result/<job_id>")
def analyze_result(job_id: str):
    with ANALYZE_JOBS_LOCK:
        job = ANALYZE_JOBS.get(job_id)
    if not job or job.get("state") != "done" or not job.get("result"):
        return render_template(
            "index.html",
            result=None,
            inspect=None,
            analysis=None,
            check=None,
            selected_snapshot=None,
            target_url=job.get("target_url", "") if job else "",
            output_root=job.get("output_root", str(OUTPUT_ROOT_DIR)) if job else str(OUTPUT_ROOT_DIR),
            error=(job.get("error") if job else "Analyze job not found") or "Analyze job not completed",
        )
    analysis = job["result"]
    disp = int(job.get("display_limit", 10))
    depth = int(job.get("cdx_limit", 1500))
    inspect = _best_inspect_for_render(job.get("target_url", ""), display_limit=disp, cdx_limit=depth)
    return render_template(
        "index.html",
        result=None,
        inspect=inspect,
        analysis=analysis,
        check=None,
        selected_snapshot=analysis.get("selected_snapshot"),
        target_url=job.get("target_url", ""),
        output_root=job.get("output_root", str(OUTPUT_ROOT_DIR)),
        error=None,
    )


@app.post("/analyze-batch/start")
def analyze_batch_start():
    target_url = _normalize_target_url(request.form.get("target_url", "").strip())
    output_root = request.form.get("output_root", str(OUTPUT_ROOT_DIR)).strip()
    display_limit = _parse_int(request.form.get("display_limit"), default=20, min_value=5, max_value=500)
    inspect_cdx_limit = _parse_int(request.form.get("inspect_cdx_limit"), default=1500, min_value=500, max_value=100000)
    analyze_cdx_limit = _parse_int(request.form.get("analyze_cdx_limit"), default=ANALYZE_DEEP_CDX_LIMIT, min_value=500, max_value=100000)
    analyze_count = _parse_int(request.form.get("analyze_count"), default=100000, min_value=1, max_value=100000)
    if not target_url:
        return jsonify({"ok": False, "error": "URL is required"}), 400
    try:
        job_id = _start_batch_analyze_job(
            target_url,
            output_root,
            display_limit,
            inspect_cdx_limit,
            analyze_cdx_limit,
            analyze_count,
        )
    except JobCapacityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 429
    return jsonify({"ok": True, "job_id": job_id})


@app.get("/analyze-batch/status/<job_id>")
def analyze_batch_status(job_id: str):
    with ANALYZE_BATCH_JOBS_LOCK:
        job = ANALYZE_BATCH_JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found"}), 404
    return jsonify({"ok": True, **job})


@app.post("/analyze-batch/pause/<job_id>")
def analyze_batch_pause(job_id: str):
    with ANALYZE_BATCH_JOBS_LOCK:
        job = ANALYZE_BATCH_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["paused"] = True
        job["state"] = "paused"
    return jsonify({"ok": True})


@app.post("/analyze-batch/resume/<job_id>")
def analyze_batch_resume(job_id: str):
    with ANALYZE_BATCH_JOBS_LOCK:
        job = ANALYZE_BATCH_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["paused"] = False
        if job.get("state") == "paused":
            job["state"] = "running"
    return jsonify({"ok": True})


@app.post("/analyze-batch/stop/<job_id>")
def analyze_batch_stop(job_id: str):
    with ANALYZE_BATCH_JOBS_LOCK:
        job = ANALYZE_BATCH_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["cancelled"] = True
        job["paused"] = False
        job["state"] = "stopping"
    return jsonify({"ok": True})


@app.post("/check/start")
def check_start():
    target_url = _normalize_target_url(request.form.get("target_url", "").strip())
    selected_snapshot = request.form.get("selected_snapshot", "").strip()
    output_root = request.form.get("output_root", str(OUTPUT_ROOT_DIR)).strip()
    if not target_url:
        return jsonify({"ok": False, "error": "URL is required"}), 400
    try:
        job_id = _start_check_job(target_url, selected_snapshot, output_root)
    except JobCapacityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 429
    return jsonify({"ok": True, "job_id": job_id})


@app.get("/check/status/<job_id>")
def check_status(job_id: str):
    with CHECK_JOBS_LOCK:
        job = CHECK_JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found"}), 404
    return jsonify({"ok": True, **job})


@app.post("/check/pause/<job_id>")
def check_pause(job_id: str):
    with CHECK_JOBS_LOCK:
        job = CHECK_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["paused"] = True
        job["state"] = "paused"
    return jsonify({"ok": True})


@app.post("/check/resume/<job_id>")
def check_resume(job_id: str):
    with CHECK_JOBS_LOCK:
        job = CHECK_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["paused"] = False
        if job.get("state") == "paused":
            job["state"] = "running"
    return jsonify({"ok": True})


@app.post("/check/stop/<job_id>")
def check_stop(job_id: str):
    with CHECK_JOBS_LOCK:
        job = CHECK_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["cancelled"] = True
        job["paused"] = False
        job["state"] = "stopping"
    return jsonify({"ok": True})


@app.get("/check/result/<job_id>")
def check_result(job_id: str):
    with CHECK_JOBS_LOCK:
        job = CHECK_JOBS.get(job_id)
    if not job or job.get("state") != "done" or not job.get("result"):
        return render_template(
            "index.html",
            result=None,
            inspect=None,
            analysis=None,
            check=None,
            selected_snapshot=None,
            target_url=job.get("target_url", "") if job else "",
            output_root=job.get("output_root", str(OUTPUT_ROOT_DIR)) if job else str(OUTPUT_ROOT_DIR),
            error=(job.get("error") if job else "Check job not found") or "Check job not completed",
        )
    check = job["result"]
    inspect = _best_inspect_for_render(job.get("target_url", ""))
    analysis = _best_analyze_for_render(job.get("target_url", ""), selected_snapshot=job.get("selected_snapshot", ""))
    return render_template(
        "index.html",
        result=None,
        inspect=inspect,
        analysis=analysis,
        check=check,
        selected_snapshot=job.get("selected_snapshot", ""),
        target_url=job.get("target_url", ""),
        output_root=job.get("output_root", str(OUTPUT_ROOT_DIR)),
        error=None,
    )


@app.post("/sitemap/start")
def sitemap_start():
    target_url = _normalize_target_url(request.form.get("target_url", "").strip())
    selected_snapshot = request.form.get("selected_snapshot", "").strip()
    output_root = request.form.get("output_root", str(OUTPUT_ROOT_DIR)).strip()
    cdx_limit = _parse_int(request.form.get("cdx_limit"), default=1500, min_value=500, max_value=100000)
    if not target_url:
        return jsonify({"ok": False, "error": "URL is required"}), 400
    try:
        job_id = _start_sitemap_job(target_url, selected_snapshot, output_root, cdx_limit)
    except JobCapacityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 429
    return jsonify({"ok": True, "job_id": job_id})


@app.get("/sitemap/status/<job_id>")
def sitemap_status(job_id: str):
    with SITEMAP_JOBS_LOCK:
        job = SITEMAP_JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found"}), 404
    return jsonify({"ok": True, **job})


@app.post("/sitemap/pause/<job_id>")
def sitemap_pause(job_id: str):
    with SITEMAP_JOBS_LOCK:
        job = SITEMAP_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["paused"] = True
        job["state"] = "paused"
    return jsonify({"ok": True})


@app.post("/sitemap/resume/<job_id>")
def sitemap_resume(job_id: str):
    with SITEMAP_JOBS_LOCK:
        job = SITEMAP_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["paused"] = False
        if job.get("state") == "paused":
            job["state"] = "running"
    return jsonify({"ok": True})


@app.post("/sitemap/stop/<job_id>")
def sitemap_stop(job_id: str):
    with SITEMAP_JOBS_LOCK:
        job = SITEMAP_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["cancelled"] = True
        job["paused"] = False
        job["state"] = "stopping"
    return jsonify({"ok": True})


@app.get("/sitemap/result/<job_id>")
def sitemap_result(job_id: str):
    with SITEMAP_JOBS_LOCK:
        job = SITEMAP_JOBS.get(job_id)
    if not job or job.get("state") != "done" or not job.get("result"):
        return render_template(
            "index.html",
            result=None,
            inspect=None,
            analysis=None,
            check=None,
            selected_snapshot=None,
            target_url=job.get("target_url", "") if job else "",
            output_root=job.get("output_root", str(OUTPUT_ROOT_DIR)) if job else str(OUTPUT_ROOT_DIR),
            error=(job.get("error") if job else "Sitemap job not found") or "Sitemap job not completed",
        )
    analysis = job["result"].get("analysis")
    sitemap = job["result"].get("sitemap")
    inspect = _best_inspect_for_render(job.get("target_url", ""))
    if analysis is None:
        analysis = _best_analyze_for_render(job.get("target_url", ""), selected_snapshot=job.get("selected_snapshot", ""))
    return render_template(
        "index.html",
        result=None,
        inspect=inspect,
        analysis=analysis,
        check=None,
        sitemap=sitemap,
        selected_snapshot=job.get("selected_snapshot", ""),
        target_url=job.get("target_url", ""),
        output_root=job.get("output_root", str(OUTPUT_ROOT_DIR)),
        error=None,
    )


@app.get("/inspect/result/<job_id>")
def inspect_result(job_id: str):
    with INSPECT_JOBS_LOCK:
        job = INSPECT_JOBS.get(job_id)
    if not job:
        return render_template(
            "index.html",
            result=None,
            inspect=None,
            analysis=None,
            check=None,
            selected_snapshot=None,
            target_url="",
            output_root=str(OUTPUT_ROOT_DIR),
            error="Inspect job not found",
        )

    if job.get("state") != "done" or not job.get("result"):
        return render_template(
            "index.html",
            result=None,
            inspect=None,
            analysis=None,
            check=None,
            selected_snapshot=None,
            target_url=job.get("target_url", ""),
            output_root=job.get("output_root", str(OUTPUT_ROOT_DIR)),
            display_limit=job.get("display_limit", 120),
            cdx_limit=job.get("cdx_limit", 20000),
            error=job.get("error") or "Inspect job not completed yet",
        )

    inspect = job["result"]
    selected_snapshot = inspect.get("latest_ok_snapshot") if inspect.get("snapshots") else None
    return render_template(
        "index.html",
        result=None,
        inspect=inspect,
        analysis=None,
        check=None,
        selected_snapshot=selected_snapshot,
        target_url=job.get("target_url", ""),
        output_root=job.get("output_root", str(OUTPUT_ROOT_DIR)),
        display_limit=job.get("display_limit", 120),
        cdx_limit=job.get("cdx_limit", 20000),
        error=None,
    )


@app.post("/analyze")
def analyze_target():
    target_url = _normalize_target_url(request.form.get("target_url", "").strip())
    selected_snapshot = request.form.get("selected_snapshot", "").strip()
    output_root = request.form.get("output_root", str(OUTPUT_ROOT_DIR)).strip()
    display_limit = _parse_int(request.form.get("display_limit"), default=10, min_value=5, max_value=2000)
    cdx_limit = _parse_int(request.form.get("cdx_limit"), default=ANALYZE_DEEP_CDX_LIMIT, min_value=500, max_value=100000)

    try:
        inspect = _cache_get(INSPECT_CACHE, f"i|{target_url}|{display_limit}|{cdx_limit}")
        if inspect is None:
            inspect = _cached_inspect(target_url, display_limit, cdx_limit)
        analysis = _cached_analyze(target_url, selected_snapshot, cdx_limit=cdx_limit)
    except Exception as exc:
        return render_template(
            "index.html",
            result=None,
            inspect=None,
            analysis=None,
            check=None,
            selected_snapshot=selected_snapshot or None,
            target_url=target_url,
            output_root=output_root,
            display_limit=display_limit,
            cdx_limit=cdx_limit,
            error=str(exc),
        )

    return render_template(
        "index.html",
        result=None,
        inspect=inspect,
        analysis=analysis,
        check=None,
        selected_snapshot=selected_snapshot,
        target_url=target_url,
        output_root=output_root,
        display_limit=display_limit,
        cdx_limit=cdx_limit,
        error=None,
    )


@app.post("/check")
def check_target():
    target_url = _normalize_target_url(request.form.get("target_url", "").strip())
    selected_snapshot = request.form.get("selected_snapshot", "").strip()
    output_root_input = request.form.get("output_root", str(OUTPUT_ROOT_DIR)).strip()

    try:
        output_root = _resolve_output_root(output_root_input)
        inspect = _cached_inspect(target_url, 10, 1500)
        analysis = _cached_analyze(target_url, selected_snapshot)
        check = tool.audit(target_url, str(output_root), selected_snapshot)
    except Exception as exc:
        return render_template(
            "index.html",
            result=None,
            inspect=None,
            analysis=None,
            check=None,
            selected_snapshot=selected_snapshot or None,
            target_url=target_url,
            output_root=output_root_input,
            error=str(exc),
        )

    return render_template(
        "index.html",
        result=None,
        inspect=inspect,
        analysis=analysis,
        check=check,
        selected_snapshot=selected_snapshot,
        target_url=target_url,
        output_root=str(output_root),
        error=None,
    )


@app.post("/sitemap")
def sitemap_target():
    target_url = _normalize_target_url(request.form.get("target_url", "").strip())
    selected_snapshot = request.form.get("selected_snapshot", "").strip()
    output_root_input = request.form.get("output_root", str(OUTPUT_ROOT_DIR)).strip()

    try:
        output_root = _resolve_output_root(output_root_input)
        inspect = _cached_inspect(target_url, 10, 1500)
        analysis = _cached_analyze(target_url, selected_snapshot)
        site_key = f"s|{target_url}|{analysis.get('selected_snapshot','')}"
        sitemap = store.get_sitemap_cache(site_key, CACHE_TTL_SECONDS)
        if sitemap is None:
            sitemap = _build_sitemap_from_analysis(analysis)
            store.set_sitemap_cache(site_key, target_url, analysis.get("selected_snapshot", ""), sitemap)
        store.upsert_project(target_url, output_root=str(output_root), snapshot=analysis.get("selected_snapshot"))
    except Exception as exc:
        return render_template(
            "index.html",
            result=None,
            inspect=None,
            analysis=None,
            check=None,
            sitemap=None,
            selected_snapshot=selected_snapshot or None,
            target_url=target_url,
            output_root=output_root_input,
            error=str(exc),
        )

    return render_template(
        "index.html",
        result=None,
        inspect=inspect,
        analysis=analysis,
        check=None,
        sitemap=sitemap,
        selected_snapshot=selected_snapshot,
        target_url=target_url,
        output_root=str(output_root),
        error=None,
    )


@app.post("/sitemap/export/json")
def sitemap_export_json():
    target_url = _normalize_target_url(request.form.get("target_url", "").strip())
    selected_snapshot = request.form.get("selected_snapshot", "").strip()
    analysis = _cached_analyze(target_url, selected_snapshot)
    site_key = f"s|{target_url}|{analysis.get('selected_snapshot','')}"
    sitemap = store.get_sitemap_cache(site_key, CACHE_TTL_SECONDS)
    if sitemap is None:
        sitemap = _build_sitemap_from_analysis(analysis)
        store.set_sitemap_cache(site_key, target_url, analysis.get("selected_snapshot", ""), sitemap)
    payload = json.dumps(sitemap, indent=2)
    filename = f"sitemap_{tool._safe_name(target_url)}_{analysis.get('selected_snapshot','latest')}.json"
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.post("/sitemap/export/csv")
def sitemap_export_csv():
    target_url = _normalize_target_url(request.form.get("target_url", "").strip())
    selected_snapshot = request.form.get("selected_snapshot", "").strip()
    analysis = _cached_analyze(target_url, selected_snapshot)
    site_key = f"s|{target_url}|{analysis.get('selected_snapshot','')}"
    sitemap = store.get_sitemap_cache(site_key, CACHE_TTL_SECONDS)
    if sitemap is None:
        sitemap = _build_sitemap_from_analysis(analysis)
        store.set_sitemap_cache(site_key, target_url, analysis.get("selected_snapshot", ""), sitemap)

    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["folder", "page"])
    for group in sitemap.get("groups", []):
        folder = group.get("folder", "")
        for page in group.get("pages", []):
            writer.writerow([folder, page])

    filename = f"sitemap_{tool._safe_name(target_url)}_{analysis.get('selected_snapshot','latest')}.csv"
    return Response(
        stream.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.post("/download-missing")
def download_missing_target():
    target_url = _normalize_target_url(request.form.get("target_url", "").strip())
    selected_snapshot = request.form.get("selected_snapshot", "").strip()
    output_root_input = request.form.get("output_root", str(OUTPUT_ROOT_DIR)).strip()
    missing_limit = _parse_missing_limit(request.form.get("missing_limit"))

    try:
        output_root = _resolve_output_root(output_root_input)
        repair = tool.download_missing(
            target_url,
            str(output_root),
            selected_snapshot=selected_snapshot,
            limit=missing_limit,
        )
        inspect = _cached_inspect(target_url, 10, 1500)
        analysis = _cached_analyze(target_url, selected_snapshot)
        check = tool.audit(target_url, str(output_root), selected_snapshot)
        message = (
            f"Missing download done: added {repair['added']} files "
            f"({repair['bytes_added_human']}), failed {repair['failed']} in {repair['seconds']}s"
        )
    except Exception as exc:
        return render_template(
            "index.html",
            result=None,
            inspect=None,
            analysis=None,
            check=None,
            selected_snapshot=selected_snapshot or None,
            target_url=target_url,
            output_root=output_root_input,
            error=str(exc),
        )

    return render_template(
        "index.html",
        result=None,
        inspect=inspect,
        analysis=analysis,
        check=check,
        selected_snapshot=selected_snapshot,
        target_url=target_url,
        output_root=str(output_root),
        action_message=message,
        error=None,
    )


def _start_download_job(target_url: str, selected_snapshot: str, max_files: int, output_root_input: str) -> str:
    _claim_job_slot()
    job_id = uuid.uuid4().hex
    started_at = time.time()

    with JOBS_LOCK:
        JOBS[job_id] = {
            "state": "queued",
            "paused": False,
            "cancelled": False,
            "error": None,
            "started_at": started_at,
            "progress": _queued_progress(
                started_at,
                files_downloaded=0,
                max_files=max_files,
                bytes_downloaded=0,
                recovered_files=0,
                current_url="",
                queue_size=0,
            ),
            "result": None,
        }

    def _runner() -> None:
        try:
            output_root = _resolve_output_root(output_root_input)

            def _update(payload: dict) -> None:
                with JOBS_LOCK:
                    if job_id in JOBS:
                        if not JOBS[job_id].get("paused", False):
                            JOBS[job_id]["state"] = "running"
                        JOBS[job_id]["progress"] = _normalize_progress(payload, started_at, stage="download", message="Downloading")

            def _wait_if_paused(current_url: str) -> None:
                while True:
                    with JOBS_LOCK:
                        job = JOBS.get(job_id)
                        if not job:
                            return
                        if bool(job.get("cancelled", False)):
                            raise RuntimeError("Stopped by user")
                        is_paused = bool(job.get("paused", False))
                        if is_paused:
                            job["state"] = "paused"
                            progress = dict(job.get("progress", {}))
                            progress["message"] = "Paused by user"
                            progress["current_url"] = current_url
                            progress["current_item"] = current_url
                            job["progress"] = _normalize_progress(progress, started_at, stage="paused", message="Paused by user")
                        else:
                            if job.get("state") == "paused":
                                job["state"] = "running"
                    if not is_paused:
                        return
                    time.sleep(0.5)

            result = tool.run(
                target_url,
                str(output_root),
                max_files=max_files,
                preferred_snapshot=selected_snapshot,
                progress_callback=_update,
                wait_if_paused=_wait_if_paused,
            )
            result_payload = asdict(result)
            with JOBS_LOCK:
                size_bytes = JOBS.get(job_id, {}).get("progress", {}).get("bytes_downloaded", 0)
            result_payload["downloaded_size_bytes"] = size_bytes
            store.upsert_project(
                target_url,
                output_root=str(output_root),
                snapshot=result_payload.get("latest_snapshot"),
            )
            store.add_job_history("download", target_url, "done", snapshot=result_payload.get("latest_snapshot"), summary=result_payload)

            with JOBS_LOCK:
                if job_id in JOBS:
                    JOBS[job_id]["state"] = "done"
                    done_progress = dict(JOBS[job_id].get("progress", {}))
                    done_progress["stage"] = "done"
                    done_progress["message"] = "Download completed"
                    done_progress["percent"] = 100
                    JOBS[job_id]["progress"] = _normalize_progress(done_progress, started_at, stage="done", message="Download completed")
                    JOBS[job_id]["result"] = result_payload
        except Exception as exc:
            store.add_job_history("download", target_url, "error", snapshot=selected_snapshot or None, summary={"error": str(exc)})
            with JOBS_LOCK:
                if job_id in JOBS:
                    JOBS[job_id]["state"] = "error"
                    JOBS[job_id]["error"] = str(exc)
                    err_progress = dict(JOBS[job_id].get("progress", {}))
                    err_progress["stage"] = "error"
                    err_progress["message"] = str(exc)
                    JOBS[job_id]["progress"] = _normalize_progress(err_progress, started_at, stage="error", message=str(exc))
        finally:
            _release_job_slot()

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    return job_id


def _start_missing_job(
    target_url: str,
    selected_snapshot: str,
    output_root_input: str,
    missing_limit: int,
    skip_errors: bool,
) -> str:
    _claim_job_slot()
    job_id = uuid.uuid4().hex
    started_at = time.time()

    with MISSING_JOBS_LOCK:
        MISSING_JOBS[job_id] = {
            "state": "queued",
            "paused": False,
            "cancelled": False,
            "error": None,
            "started_at": started_at,
            "progress": _queued_progress(
                started_at,
                attempted=0,
                total=missing_limit,
                added=0,
                failed=0,
                bytes_added=0,
                current_url="",
            ),
            "result": None,
        }

    def _runner() -> None:
        try:
            output_root = _resolve_output_root(output_root_input)

            def _update(payload: dict) -> None:
                with MISSING_JOBS_LOCK:
                    if job_id in MISSING_JOBS:
                        if not MISSING_JOBS[job_id].get("paused", False):
                            MISSING_JOBS[job_id]["state"] = "running"
                        MISSING_JOBS[job_id]["progress"] = _normalize_progress(payload, started_at, stage="missing", message="Downloading missing files")

            def _should_abort() -> bool:
                with MISSING_JOBS_LOCK:
                    job = MISSING_JOBS.get(job_id)
                    if not job:
                        return True
                    return bool(job.get("cancelled", False))

            def _wait_if_paused(current_url: str) -> None:
                while True:
                    with MISSING_JOBS_LOCK:
                        job = MISSING_JOBS.get(job_id)
                        if not job:
                            return
                        if bool(job.get("cancelled", False)):
                            raise RuntimeError("Stopped by user")
                        is_paused = bool(job.get("paused", False))
                        if is_paused:
                            job["state"] = "paused"
                            progress = dict(job.get("progress", {}))
                            progress["message"] = "Paused by user"
                            progress["current_url"] = current_url
                            progress["current_item"] = current_url
                            job["progress"] = _normalize_progress(progress, started_at, stage="paused", message="Paused by user")
                        elif job.get("state") == "paused":
                            job["state"] = "running"
                    if not is_paused:
                        return
                    time.sleep(0.5)

            result = tool.download_missing(
                target_url,
                str(output_root),
                selected_snapshot=selected_snapshot,
                limit=missing_limit,
                progress_callback=_update,
                skip_errors=skip_errors,
                should_abort=_should_abort,
                wait_if_paused=_wait_if_paused,
            )
            with MISSING_JOBS_LOCK:
                if job_id in MISSING_JOBS:
                    MISSING_JOBS[job_id]["state"] = "done"
                    done_progress = dict(MISSING_JOBS[job_id].get("progress", {}))
                    done_progress["stage"] = "done"
                    done_progress["message"] = "Missing files download completed"
                    done_progress["percent"] = 100
                    MISSING_JOBS[job_id]["progress"] = _normalize_progress(done_progress, started_at, stage="done", message="Missing files download completed")
                    MISSING_JOBS[job_id]["result"] = result
            store.add_job_history("missing", target_url, "done", snapshot=result.get("snapshot"), summary=result)
        except Exception as exc:
            store.add_job_history("missing", target_url, "error", snapshot=selected_snapshot or None, summary={"error": str(exc)})
            with MISSING_JOBS_LOCK:
                if job_id in MISSING_JOBS:
                    MISSING_JOBS[job_id]["state"] = "error"
                    MISSING_JOBS[job_id]["error"] = str(exc)
                    err_progress = dict(MISSING_JOBS[job_id].get("progress", {}))
                    err_progress["stage"] = "error"
                    err_progress["message"] = str(exc)
                    MISSING_JOBS[job_id]["progress"] = _normalize_progress(err_progress, started_at, stage="error", message=str(exc))
        finally:
            _release_job_slot()

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    return job_id


def _start_inspect_job(target_url: str, output_root_input: str, display_limit: int, cdx_limit: int) -> str:
    _claim_job_slot()
    job_id = uuid.uuid4().hex
    started_at = time.time()

    with INSPECT_JOBS_LOCK:
        INSPECT_JOBS[job_id] = {
            "state": "queued",
            "paused": False,
            "cancelled": False,
            "error": None,
            "started_at": started_at,
            "target_url": target_url,
            "output_root": output_root_input,
            "display_limit": display_limit,
            "cdx_limit": cdx_limit,
            "progress": _queued_progress(
                started_at,
                variants_done=0,
                variants_total=0,
                current_variant="",
                total_captures=0,
                total_ok=0,
            ),
            "result": None,
        }

    def _runner() -> None:
        try:
            def _update(payload: dict) -> None:
                with INSPECT_JOBS_LOCK:
                    if job_id in INSPECT_JOBS:
                        if not INSPECT_JOBS[job_id].get("paused", False):
                            INSPECT_JOBS[job_id]["state"] = "running"
                        INSPECT_JOBS[job_id]["progress"] = _normalize_progress(payload, started_at, stage="inspect", message="Inspecting snapshots")

            def _wait_if_paused(current_variant: str) -> None:
                while True:
                    with INSPECT_JOBS_LOCK:
                        job = INSPECT_JOBS.get(job_id)
                        if not job:
                            return
                        if bool(job.get("cancelled", False)):
                            raise RuntimeError("Stopped by user")
                        is_paused = bool(job.get("paused", False))
                        if is_paused:
                            job["state"] = "paused"
                            progress = dict(job.get("progress", {}))
                            progress["message"] = "Paused by user"
                            progress["current_variant"] = current_variant
                            progress["current_item"] = current_variant
                            job["progress"] = _normalize_progress(progress, started_at, stage="paused", message="Paused by user")
                        elif job.get("state") == "paused":
                            job["state"] = "running"
                    if not is_paused:
                        return
                    time.sleep(0.5)

            def _should_abort() -> bool:
                with INSPECT_JOBS_LOCK:
                    job = INSPECT_JOBS.get(job_id)
                    if not job:
                        return True
                    return bool(job.get("cancelled", False))

            result = tool.inspect(
                target_url,
                progress_callback=_update,
                display_limit=display_limit,
                cdx_limit=cdx_limit,
                wait_if_paused=_wait_if_paused,
                should_abort=_should_abort,
            )
            inspect_key = f"i|{target_url}|{display_limit}|{cdx_limit}"
            store.set_inspect_cache(inspect_key, target_url, display_limit, cdx_limit, result)
            store.upsert_project(target_url, output_root=output_root_input, snapshot=result.get("latest_snapshot"))
            store.add_job_history("inspect", target_url, "done", snapshot=result.get("latest_snapshot"), summary={"total": result.get("total_snapshots")})
            with INSPECT_JOBS_LOCK:
                if job_id in INSPECT_JOBS:
                    INSPECT_JOBS[job_id]["state"] = "done"
                    done_progress = dict(INSPECT_JOBS[job_id].get("progress", {}))
                    done_progress["stage"] = "done"
                    done_progress["message"] = "Inspect completed"
                    done_progress["percent"] = 100
                    INSPECT_JOBS[job_id]["progress"] = _normalize_progress(done_progress, started_at, stage="done", message="Inspect completed")
                    INSPECT_JOBS[job_id]["result"] = _with_cache_meta(result, "archive", 0)
        except Exception as exc:
            store.add_job_history("inspect", target_url, "error", summary={"error": str(exc)})
            with INSPECT_JOBS_LOCK:
                if job_id in INSPECT_JOBS:
                    INSPECT_JOBS[job_id]["state"] = "error"
                    INSPECT_JOBS[job_id]["error"] = str(exc)
                    err_progress = dict(INSPECT_JOBS[job_id].get("progress", {}))
                    err_progress["stage"] = "error"
                    err_progress["message"] = str(exc)
                    INSPECT_JOBS[job_id]["progress"] = _normalize_progress(err_progress, started_at, stage="error", message=str(exc))
        finally:
            _release_job_slot()

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    return job_id


def _start_analyze_job(
    target_url: str,
    selected_snapshot: str,
    output_root_input: str,
    cdx_limit: int,
    display_limit: int,
) -> str:
    _claim_job_slot()
    job_id = uuid.uuid4().hex
    started_at = time.time()
    with ANALYZE_JOBS_LOCK:
        ANALYZE_JOBS[job_id] = {
            "state": "queued",
            "paused": False,
            "cancelled": False,
            "error": None,
            "started_at": started_at,
            "target_url": target_url,
            "selected_snapshot": selected_snapshot,
            "output_root": output_root_input,
            "display_limit": display_limit,
            "cdx_limit": cdx_limit,
            "progress": _queued_progress(started_at),
            "result": None,
        }

    def _runner() -> None:
        try:
            def _update(payload: dict) -> None:
                with ANALYZE_JOBS_LOCK:
                    if job_id in ANALYZE_JOBS:
                        if not ANALYZE_JOBS[job_id].get("paused", False):
                            ANALYZE_JOBS[job_id]["state"] = "running"
                        ANALYZE_JOBS[job_id]["progress"] = _normalize_progress(payload, started_at, stage="analyze", message="Analyzing snapshot")

            def _wait_if_paused(label: str) -> None:
                while True:
                    with ANALYZE_JOBS_LOCK:
                        job = ANALYZE_JOBS.get(job_id)
                        if not job:
                            return
                        if bool(job.get("cancelled", False)):
                            raise RuntimeError("Stopped by user")
                        paused = bool(job.get("paused", False))
                        if paused:
                            job["state"] = "paused"
                            p = dict(job.get("progress", {}))
                            p["message"] = "Paused by user"
                            p["label"] = label
                            p["current_item"] = label
                            job["progress"] = _normalize_progress(p, started_at, stage="paused", message="Paused by user")
                        elif job.get("state") == "paused":
                            job["state"] = "running"
                    if not paused:
                        return
                    time.sleep(0.5)

            def _should_abort() -> bool:
                with ANALYZE_JOBS_LOCK:
                    job = ANALYZE_JOBS.get(job_id)
                    if not job:
                        return True
                    return bool(job.get("cancelled", False))

            analysis = tool.analyze(
                target_url,
                selected_snapshot,
                cdx_limit=cdx_limit,
                progress_callback=_update,
                wait_if_paused=_wait_if_paused,
                should_abort=_should_abort,
            )
            analyze_key = f"a|{target_url}|{analysis.get('selected_snapshot', selected_snapshot)}|{cdx_limit}"
            store.set_analyze_cache(analyze_key, target_url, analysis.get("selected_snapshot", selected_snapshot), cdx_limit, analysis)
            store.upsert_project(
                target_url,
                output_root=output_root_input,
                snapshot=analysis.get("selected_snapshot"),
                site_type=analysis.get("site_type"),
                estimated_files=int(analysis.get("estimated_files", 0) or 0),
                estimated_size=int(analysis.get("estimated_size_bytes", 0) or 0),
            )
            store.add_job_history("analyze", target_url, "done", snapshot=analysis.get("selected_snapshot"), summary={"type": analysis.get("site_type")})
            with ANALYZE_JOBS_LOCK:
                if job_id in ANALYZE_JOBS:
                    ANALYZE_JOBS[job_id]["state"] = "done"
                    done_progress = dict(ANALYZE_JOBS[job_id].get("progress", {}))
                    done_progress["stage"] = "done"
                    done_progress["message"] = "Analyze completed"
                    done_progress["percent"] = 100
                    ANALYZE_JOBS[job_id]["progress"] = _normalize_progress(done_progress, started_at, stage="done", message="Analyze completed")
                    ANALYZE_JOBS[job_id]["result"] = _with_cache_meta(analysis, "archive", 0)
        except Exception as exc:
            store.add_job_history("analyze", target_url, "error", snapshot=selected_snapshot or None, summary={"error": str(exc)})
            with ANALYZE_JOBS_LOCK:
                if job_id in ANALYZE_JOBS:
                    ANALYZE_JOBS[job_id]["state"] = "error"
                    ANALYZE_JOBS[job_id]["error"] = str(exc)
                    err_progress = dict(ANALYZE_JOBS[job_id].get("progress", {}))
                    err_progress["stage"] = "error"
                    err_progress["message"] = str(exc)
                    ANALYZE_JOBS[job_id]["progress"] = _normalize_progress(err_progress, started_at, stage="error", message=str(exc))
        finally:
            _release_job_slot()

    threading.Thread(target=_runner, daemon=True).start()
    return job_id


def _start_batch_analyze_job(
    target_url: str,
    output_root_input: str,
    display_limit: int,
    inspect_cdx_limit: int,
    analyze_cdx_limit: int,
    analyze_count: int,
) -> str:
    _claim_job_slot()
    job_id = uuid.uuid4().hex
    started_at = time.time()
    with ANALYZE_BATCH_JOBS_LOCK:
        ANALYZE_BATCH_JOBS[job_id] = {
            "state": "queued",
            "paused": False,
            "cancelled": False,
            "error": None,
            "started_at": started_at,
            "target_url": target_url,
            "output_root": output_root_input,
            "display_limit": display_limit,
            "inspect_cdx_limit": inspect_cdx_limit,
            "analyze_cdx_limit": analyze_cdx_limit,
            "analyze_count": analyze_count,
            "progress": _queued_progress(
                started_at,
                done=0,
                total=analyze_count,
                current_snapshot="",
                last_site_type="",
            ),
            "result": None,
        }

    def _runner() -> None:
        try:
            inspect = _cached_inspect(target_url, display_limit, inspect_cdx_limit)
            snapshots = list(inspect.get("snapshots", []))
            if analyze_count > 0:
                snapshots = snapshots[:analyze_count]
            if not snapshots:
                raise RuntimeError("No snapshots available for one-by-one analysis")

            total = len(snapshots)
            done = 0
            analyzed: list[dict] = []

            def _wait_if_paused(current_snapshot: str) -> None:
                while True:
                    with ANALYZE_BATCH_JOBS_LOCK:
                        job = ANALYZE_BATCH_JOBS.get(job_id)
                        if not job:
                            return
                        if bool(job.get("cancelled", False)):
                            raise RuntimeError("Stopped by user")
                        paused = bool(job.get("paused", False))
                        if paused:
                            job["state"] = "paused"
                            p = dict(job.get("progress", {}))
                            p["message"] = "Paused by user"
                            p["current_snapshot"] = current_snapshot
                            p["current_item"] = current_snapshot
                            job["progress"] = _normalize_progress(p, started_at, stage="paused", message="Paused by user")
                        elif job.get("state") == "paused":
                            job["state"] = "running"
                    if not paused:
                        return
                    time.sleep(0.5)

            for idx, ts in enumerate(snapshots, start=1):
                _wait_if_paused(ts)
                with ANALYZE_BATCH_JOBS_LOCK:
                    if job_id in ANALYZE_BATCH_JOBS:
                        ANALYZE_BATCH_JOBS[job_id]["state"] = "running"
                        ANALYZE_BATCH_JOBS[job_id]["progress"] = _normalize_progress({
                            "stage": "analyze",
                            "message": "Analyzing snapshots one-by-one",
                            "percent": min(98, int((done / total) * 100)),
                            "done": done,
                            "total": total,
                            "current_snapshot": ts,
                            "current_item": ts,
                            "last_site_type": analyzed[-1]["site_type"] if analyzed else "",
                        }, started_at, stage="analyze", message="Analyzing snapshots one-by-one")

                cached = store.get_latest_analyze_for_url(
                    target_url,
                    PERSISTENT_CACHE_MAX_AGE_SECONDS,
                    snapshot=ts,
                )
                if cached is not None:
                    data = _with_cache_meta(cached.get("payload", {}), "sqlite", int(cached.get("age_seconds", 0)))
                else:
                    data = _cached_analyze(target_url, ts, cdx_limit=analyze_cdx_limit)
                analyzed.append(
                    {
                        "snapshot": data.get("selected_snapshot", ts),
                        "site_type": data.get("site_type", "Unknown"),
                        "estimated_files": int(data.get("estimated_files", 0) or 0),
                        "estimated_size_bytes": int(data.get("estimated_size_bytes", 0) or 0),
                        "source": (data.get("_cache") or {}).get("source", "archive"),
                    }
                )
                done += 1

            result = {
                "target_url": target_url,
                "total_requested": analyze_count,
                "total_analyzed": done,
                "snapshots": analyzed,
            }
            store.add_job_history("analyze_batch", target_url, "done", summary=result)
            with ANALYZE_BATCH_JOBS_LOCK:
                if job_id in ANALYZE_BATCH_JOBS:
                    ANALYZE_BATCH_JOBS[job_id]["state"] = "done"
                    ANALYZE_BATCH_JOBS[job_id]["progress"] = _normalize_progress({
                        "stage": "done",
                        "message": "One-by-one analysis completed",
                        "percent": 100,
                        "done": done,
                        "total": total,
                        "current_snapshot": "",
                        "current_item": "",
                        "last_site_type": analyzed[-1]["site_type"] if analyzed else "",
                    }, started_at, stage="done", message="One-by-one analysis completed")
                    ANALYZE_BATCH_JOBS[job_id]["result"] = result
        except Exception as exc:
            store.add_job_history("analyze_batch", target_url, "error", summary={"error": str(exc)})
            with ANALYZE_BATCH_JOBS_LOCK:
                if job_id in ANALYZE_BATCH_JOBS:
                    ANALYZE_BATCH_JOBS[job_id]["state"] = "error"
                    ANALYZE_BATCH_JOBS[job_id]["error"] = str(exc)
                    err_progress = dict(ANALYZE_BATCH_JOBS[job_id].get("progress", {}))
                    err_progress["stage"] = "error"
                    err_progress["message"] = str(exc)
                    ANALYZE_BATCH_JOBS[job_id]["progress"] = _normalize_progress(err_progress, started_at, stage="error", message=str(exc))
        finally:
            _release_job_slot()

    threading.Thread(target=_runner, daemon=True).start()
    return job_id


def _start_check_job(target_url: str, selected_snapshot: str, output_root_input: str) -> str:
    _claim_job_slot()
    job_id = uuid.uuid4().hex
    started_at = time.time()
    with CHECK_JOBS_LOCK:
        CHECK_JOBS[job_id] = {
            "state": "queued",
            "paused": False,
            "cancelled": False,
            "error": None,
            "started_at": started_at,
            "target_url": target_url,
            "selected_snapshot": selected_snapshot,
            "output_root": output_root_input,
            "progress": _queued_progress(started_at),
            "result": None,
        }

    def _runner() -> None:
        try:
            output_root = _resolve_output_root(output_root_input)

            def _update(payload: dict) -> None:
                with CHECK_JOBS_LOCK:
                    if job_id in CHECK_JOBS:
                        if not CHECK_JOBS[job_id].get("paused", False):
                            CHECK_JOBS[job_id]["state"] = "running"
                        CHECK_JOBS[job_id]["progress"] = _normalize_progress(payload, started_at, stage="check", message="Checking files")

            def _wait_if_paused(label: str) -> None:
                while True:
                    with CHECK_JOBS_LOCK:
                        job = CHECK_JOBS.get(job_id)
                        if not job:
                            return
                        if bool(job.get("cancelled", False)):
                            raise RuntimeError("Stopped by user")
                        paused = bool(job.get("paused", False))
                        if paused:
                            job["state"] = "paused"
                            p = dict(job.get("progress", {}))
                            p["message"] = "Paused by user"
                            p["label"] = label
                            p["current_item"] = label
                            job["progress"] = _normalize_progress(p, started_at, stage="paused", message="Paused by user")
                        elif job.get("state") == "paused":
                            job["state"] = "running"
                    if not paused:
                        return
                    time.sleep(0.5)

            def _should_abort() -> bool:
                with CHECK_JOBS_LOCK:
                    job = CHECK_JOBS.get(job_id)
                    if not job:
                        return True
                    return bool(job.get("cancelled", False))

            check = tool.audit(
                target_url,
                str(output_root),
                selected_snapshot,
                progress_callback=_update,
                wait_if_paused=_wait_if_paused,
                should_abort=_should_abort,
            )
            store.upsert_project(target_url, output_root=str(output_root), snapshot=check.get("snapshot"))
            store.add_job_history("check", target_url, "done", snapshot=check.get("snapshot"), summary={"coverage": check.get("coverage_percent")})
            with CHECK_JOBS_LOCK:
                if job_id in CHECK_JOBS:
                    CHECK_JOBS[job_id]["state"] = "done"
                    done_progress = dict(CHECK_JOBS[job_id].get("progress", {}))
                    done_progress["stage"] = "done"
                    done_progress["message"] = "Check completed"
                    done_progress["percent"] = 100
                    CHECK_JOBS[job_id]["progress"] = _normalize_progress(done_progress, started_at, stage="done", message="Check completed")
                    CHECK_JOBS[job_id]["result"] = check
        except Exception as exc:
            store.add_job_history("check", target_url, "error", snapshot=selected_snapshot or None, summary={"error": str(exc)})
            with CHECK_JOBS_LOCK:
                if job_id in CHECK_JOBS:
                    CHECK_JOBS[job_id]["state"] = "error"
                    CHECK_JOBS[job_id]["error"] = str(exc)
                    err_progress = dict(CHECK_JOBS[job_id].get("progress", {}))
                    err_progress["stage"] = "error"
                    err_progress["message"] = str(exc)
                    CHECK_JOBS[job_id]["progress"] = _normalize_progress(err_progress, started_at, stage="error", message=str(exc))
        finally:
            _release_job_slot()

    threading.Thread(target=_runner, daemon=True).start()
    return job_id


def _start_sitemap_job(target_url: str, selected_snapshot: str, output_root_input: str, cdx_limit: int) -> str:
    _claim_job_slot()
    job_id = uuid.uuid4().hex
    started_at = time.time()
    with SITEMAP_JOBS_LOCK:
        SITEMAP_JOBS[job_id] = {
            "state": "queued",
            "paused": False,
            "cancelled": False,
            "error": None,
            "started_at": started_at,
            "target_url": target_url,
            "selected_snapshot": selected_snapshot,
            "output_root": output_root_input,
            "cdx_limit": cdx_limit,
            "progress": _queued_progress(started_at),
            "result": None,
        }

    def _runner() -> None:
        try:
            def _update(payload: dict) -> None:
                with SITEMAP_JOBS_LOCK:
                    if job_id in SITEMAP_JOBS:
                        if not SITEMAP_JOBS[job_id].get("paused", False):
                            SITEMAP_JOBS[job_id]["state"] = "running"
                        SITEMAP_JOBS[job_id]["progress"] = _normalize_progress(payload, started_at, stage="sitemap", message="Building sitemap")

            def _wait_if_paused(label: str) -> None:
                while True:
                    with SITEMAP_JOBS_LOCK:
                        job = SITEMAP_JOBS.get(job_id)
                        if not job:
                            return
                        if bool(job.get("cancelled", False)):
                            raise RuntimeError("Stopped by user")
                        paused = bool(job.get("paused", False))
                        if paused:
                            job["state"] = "paused"
                            p = dict(job.get("progress", {}))
                            p["message"] = "Paused by user"
                            p["label"] = label
                            p["current_item"] = label
                            job["progress"] = _normalize_progress(p, started_at, stage="paused", message="Paused by user")
                        elif job.get("state") == "paused":
                            job["state"] = "running"
                    if not paused:
                        return
                    time.sleep(0.5)

            def _should_abort() -> bool:
                with SITEMAP_JOBS_LOCK:
                    job = SITEMAP_JOBS.get(job_id)
                    if not job:
                        return True
                    return bool(job.get("cancelled", False))

            analysis = tool.analyze(
                target_url,
                selected_snapshot,
                cdx_limit=cdx_limit,
                progress_callback=_update,
                wait_if_paused=_wait_if_paused,
                should_abort=_should_abort,
            )
            sitemap = _build_sitemap_from_analysis(analysis)
            site_key = f"s|{target_url}|{analysis.get('selected_snapshot','')}"
            store.set_sitemap_cache(site_key, target_url, analysis.get("selected_snapshot", ""), sitemap)
            store.upsert_project(target_url, output_root=output_root_input, snapshot=analysis.get("selected_snapshot"))
            store.add_job_history("sitemap", target_url, "done", snapshot=analysis.get("selected_snapshot"), summary={"pages": sitemap.get("total_pages")})
            with SITEMAP_JOBS_LOCK:
                if job_id in SITEMAP_JOBS:
                    SITEMAP_JOBS[job_id]["state"] = "done"
                    done_progress = dict(SITEMAP_JOBS[job_id].get("progress", {}))
                    done_progress["stage"] = "done"
                    done_progress["message"] = "Sitemap completed"
                    done_progress["percent"] = 100
                    SITEMAP_JOBS[job_id]["progress"] = _normalize_progress(done_progress, started_at, stage="done", message="Sitemap completed")
                    SITEMAP_JOBS[job_id]["result"] = {"analysis": _with_cache_meta(analysis, "archive", 0), "sitemap": sitemap}
        except Exception as exc:
            store.add_job_history("sitemap", target_url, "error", snapshot=selected_snapshot or None, summary={"error": str(exc)})
            with SITEMAP_JOBS_LOCK:
                if job_id in SITEMAP_JOBS:
                    SITEMAP_JOBS[job_id]["state"] = "error"
                    SITEMAP_JOBS[job_id]["error"] = str(exc)
                    err_progress = dict(SITEMAP_JOBS[job_id].get("progress", {}))
                    err_progress["stage"] = "error"
                    err_progress["message"] = str(exc)
                    SITEMAP_JOBS[job_id]["progress"] = _normalize_progress(err_progress, started_at, stage="error", message=str(exc))
        finally:
            _release_job_slot()

    threading.Thread(target=_runner, daemon=True).start()
    return job_id


@app.post("/download/start")
def download_start():
    target_url = _normalize_target_url(request.form.get("target_url", "").strip())
    selected_snapshot = request.form.get("selected_snapshot", "").strip()
    max_files = _parse_max_files(request.form.get("max_files"))
    output_root_input = request.form.get("output_root", str(OUTPUT_ROOT_DIR)).strip()

    if not target_url:
        return jsonify({"ok": False, "error": "URL is required"}), 400

    try:
        job_id = _start_download_job(target_url, selected_snapshot, max_files, output_root_input)
    except JobCapacityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 429
    return jsonify({"ok": True, "job_id": job_id})


@app.get("/download/status/<job_id>")
def download_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found"}), 404
    return jsonify({"ok": True, **job})


@app.post("/download/stop/<job_id>")
def download_stop(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["cancelled"] = True
        job["paused"] = False
        job["state"] = "stopping"
    return jsonify({"ok": True})


@app.post("/download-missing/start")
def download_missing_start():
    target_url = _normalize_target_url(request.form.get("target_url", "").strip())
    selected_snapshot = request.form.get("selected_snapshot", "").strip()
    output_root_input = request.form.get("output_root", str(OUTPUT_ROOT_DIR)).strip()
    missing_limit = _parse_missing_limit(request.form.get("missing_limit"))
    skip_errors = _parse_bool(request.form.get("skip_errors"), default=True)

    if not target_url:
        return jsonify({"ok": False, "error": "URL is required"}), 400

    try:
        job_id = _start_missing_job(target_url, selected_snapshot, output_root_input, missing_limit, skip_errors)
    except JobCapacityError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 429
    return jsonify({"ok": True, "job_id": job_id})


@app.get("/download-missing/status/<job_id>")
def download_missing_status(job_id: str):
    with MISSING_JOBS_LOCK:
        job = MISSING_JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found"}), 404
    return jsonify({"ok": True, **job})


@app.post("/download-missing/stop/<job_id>")
def download_missing_stop(job_id: str):
    with MISSING_JOBS_LOCK:
        job = MISSING_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["cancelled"] = True
        job["paused"] = False
        job["state"] = "stopping"
    return jsonify({"ok": True})


@app.post("/download-missing/pause/<job_id>")
def download_missing_pause(job_id: str):
    with MISSING_JOBS_LOCK:
        job = MISSING_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["paused"] = True
        job["state"] = "paused"
    return jsonify({"ok": True})


@app.post("/download-missing/resume/<job_id>")
def download_missing_resume(job_id: str):
    with MISSING_JOBS_LOCK:
        job = MISSING_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["paused"] = False
        if job.get("state") == "paused":
            job["state"] = "running"
    return jsonify({"ok": True})


@app.post("/download/pause/<job_id>")
def download_pause(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["paused"] = True
        job["state"] = "paused"
    return jsonify({"ok": True})


@app.post("/download/resume/<job_id>")
def download_resume(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("state") in ("done", "error"):
            return jsonify({"ok": False, "error": "Job already finished"}), 400
        job["paused"] = False
        if job.get("state") == "paused":
            job["state"] = "running"
        progress = dict(job.get("progress", {}))
        progress["message"] = "Resuming..."
        job["progress"] = progress
    return jsonify({"ok": True})


@app.post("/download")
def download_target():
    target_url = _normalize_target_url(request.form.get("target_url", "").strip())
    selected_snapshot = request.form.get("selected_snapshot", "").strip()
    max_files = _parse_max_files(request.form.get("max_files"))
    output_root_input = request.form.get("output_root", str(OUTPUT_ROOT_DIR)).strip()

    try:
        output_root = _resolve_output_root(output_root_input)
        inspect = _cached_inspect(target_url, 10, 1500)
        analysis = _cached_analyze(target_url, selected_snapshot)
        result = tool.run(
            target_url,
            str(output_root),
            max_files=max_files,
            preferred_snapshot=selected_snapshot,
        )
    except Exception as exc:
        return render_template(
            "index.html",
            result=None,
            inspect=None,
            analysis=None,
            check=None,
            selected_snapshot=selected_snapshot or None,
            target_url=target_url,
            output_root=output_root_input,
            error=str(exc),
        )

    rel_output = os.path.relpath(result.output_dir, str(BASE_DIR)).replace("\\", "/")
    return render_template(
        "index.html",
        result=result,
        inspect=inspect,
        analysis=analysis,
        check=None,
        selected_snapshot=selected_snapshot,
        target_url=target_url,
        output_root=str(output_root),
        rel_output=rel_output,
        error=None,
    )


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    debug = _parse_bool(os.environ.get("FLASK_DEBUG"), default=False)
    app.run(host=host, port=port, debug=debug, use_reloader=False, threaded=True)
