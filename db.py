from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


class SQLiteStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    target_url TEXT PRIMARY KEY,
                    domain TEXT NOT NULL,
                    last_output_root TEXT,
                    last_snapshot TEXT,
                    last_site_type TEXT,
                    last_estimated_files INTEGER,
                    last_estimated_size INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inspect_cache (
                    cache_key TEXT PRIMARY KEY,
                    target_url TEXT NOT NULL,
                    display_limit INTEGER NOT NULL,
                    cdx_limit INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS analyze_cache (
                    cache_key TEXT PRIMARY KEY,
                    target_url TEXT NOT NULL,
                    snapshot TEXT,
                    cdx_limit INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sitemap_cache (
                    cache_key TEXT PRIMARY KEY,
                    target_url TEXT NOT NULL,
                    snapshot TEXT,
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_type TEXT NOT NULL,
                    target_url TEXT NOT NULL,
                    snapshot TEXT,
                    state TEXT NOT NULL,
                    summary_json TEXT,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_inspect_cache_target_created ON inspect_cache(target_url, created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_analyze_cache_target_snapshot_created ON analyze_cache(target_url, snapshot, created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sitemap_cache_target_snapshot_created ON sitemap_cache(target_url, snapshot, created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_history_target_created ON jobs_history(target_url, created_at DESC)")

    def _normalize_target_url(self, target_url: str) -> str:
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

    def _extract_domain(self, target_url: str) -> str:
        normalized = self._normalize_target_url(target_url)
        parsed = urlparse(normalized if "://" in normalized else f"https://{normalized}")
        host = (parsed.netloc or parsed.path or "").strip().lower()
        if host.startswith("www."):
            host = host[4:]
        return host

    def _target_url_variants(self, target_url: str) -> List[str]:
        host = self._extract_domain(target_url)
        if not host:
            normalized = self._normalize_target_url(target_url)
            return [normalized] if normalized else []
        return [
            f"https://{host}",
            f"http://{host}",
            f"https://www.{host}",
            f"http://www.{host}",
        ]

    def get_inspect_cache(self, cache_key: str, max_age_seconds: int) -> Optional[Dict[str, Any]]:
        row = self._get_cache_row_with_meta("inspect_cache", cache_key, max_age_seconds)
        return row["payload"] if row else None

    def get_inspect_cache_with_meta(self, cache_key: str, max_age_seconds: int) -> Optional[Dict[str, Any]]:
        return self._get_cache_row_with_meta("inspect_cache", cache_key, max_age_seconds)

    def set_inspect_cache(
        self,
        cache_key: str,
        target_url: str,
        display_limit: int,
        cdx_limit: int,
        payload: Dict[str, Any],
    ) -> None:
        target_url = self._normalize_target_url(target_url)
        now = int(time.time())
        data = json.dumps(payload)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inspect_cache(cache_key,target_url,display_limit,cdx_limit,payload_json,created_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    target_url=excluded.target_url,
                    display_limit=excluded.display_limit,
                    cdx_limit=excluded.cdx_limit,
                    payload_json=excluded.payload_json,
                    created_at=excluded.created_at
                """,
                (cache_key, target_url, display_limit, cdx_limit, data, now),
            )

    def get_analyze_cache(self, cache_key: str, max_age_seconds: int) -> Optional[Dict[str, Any]]:
        row = self._get_cache_row_with_meta("analyze_cache", cache_key, max_age_seconds)
        return row["payload"] if row else None

    def get_analyze_cache_with_meta(self, cache_key: str, max_age_seconds: int) -> Optional[Dict[str, Any]]:
        return self._get_cache_row_with_meta("analyze_cache", cache_key, max_age_seconds)

    def set_analyze_cache(
        self,
        cache_key: str,
        target_url: str,
        snapshot: str,
        cdx_limit: int,
        payload: Dict[str, Any],
    ) -> None:
        target_url = self._normalize_target_url(target_url)
        now = int(time.time())
        data = json.dumps(payload)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO analyze_cache(cache_key,target_url,snapshot,cdx_limit,payload_json,created_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    target_url=excluded.target_url,
                    snapshot=excluded.snapshot,
                    cdx_limit=excluded.cdx_limit,
                    payload_json=excluded.payload_json,
                    created_at=excluded.created_at
                """,
                (cache_key, target_url, snapshot, cdx_limit, data, now),
            )

    def get_sitemap_cache(self, cache_key: str, max_age_seconds: int) -> Optional[Dict[str, Any]]:
        row = self._get_cache_row_with_meta("sitemap_cache", cache_key, max_age_seconds)
        return row["payload"] if row else None

    def get_sitemap_cache_with_meta(self, cache_key: str, max_age_seconds: int) -> Optional[Dict[str, Any]]:
        return self._get_cache_row_with_meta("sitemap_cache", cache_key, max_age_seconds)

    def get_latest_inspect_for_url(self, target_url: str, max_age_seconds: int) -> Optional[Dict[str, Any]]:
        target_url = self._normalize_target_url(target_url)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT cache_key, payload_json, created_at
                FROM inspect_cache
                WHERE target_url = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (target_url,),
            ).fetchone()
        return self._decode_cache_row(row, max_age_seconds)

    def get_latest_analyze_for_url(
        self,
        target_url: str,
        max_age_seconds: int,
        snapshot: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        target_url = self._normalize_target_url(target_url)
        with self._connect() as conn:
            if snapshot:
                row = conn.execute(
                    """
                    SELECT cache_key, payload_json, created_at
                    FROM analyze_cache
                    WHERE target_url = ? AND snapshot = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (target_url, snapshot),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT cache_key, payload_json, created_at
                    FROM analyze_cache
                    WHERE target_url = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (target_url,),
                ).fetchone()
        return self._decode_cache_row(row, max_age_seconds)

    def set_sitemap_cache(self, cache_key: str, target_url: str, snapshot: str, payload: Dict[str, Any]) -> None:
        target_url = self._normalize_target_url(target_url)
        now = int(time.time())
        data = json.dumps(payload)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sitemap_cache(cache_key,target_url,snapshot,payload_json,created_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    target_url=excluded.target_url,
                    snapshot=excluded.snapshot,
                    payload_json=excluded.payload_json,
                    created_at=excluded.created_at
                """,
                (cache_key, target_url, snapshot, data, now),
            )

    def upsert_project(
        self,
        target_url: str,
        output_root: Optional[str] = None,
        snapshot: Optional[str] = None,
        site_type: Optional[str] = None,
        estimated_files: Optional[int] = None,
        estimated_size: Optional[int] = None,
    ) -> None:
        target_url = self._normalize_target_url(target_url)
        now = int(time.time())
        domain = urlparse(target_url).netloc or target_url
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO projects(
                    target_url,domain,last_output_root,last_snapshot,last_site_type,
                    last_estimated_files,last_estimated_size,created_at,updated_at
                )
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(target_url) DO UPDATE SET
                    domain=excluded.domain,
                    last_output_root=COALESCE(excluded.last_output_root, projects.last_output_root),
                    last_snapshot=COALESCE(excluded.last_snapshot, projects.last_snapshot),
                    last_site_type=COALESCE(excluded.last_site_type, projects.last_site_type),
                    last_estimated_files=COALESCE(excluded.last_estimated_files, projects.last_estimated_files),
                    last_estimated_size=COALESCE(excluded.last_estimated_size, projects.last_estimated_size),
                    updated_at=excluded.updated_at
                """,
                (
                    target_url,
                    domain,
                    output_root,
                    snapshot,
                    site_type,
                    estimated_files,
                    estimated_size,
                    now,
                    now,
                ),
            )

    def list_recent_projects(self, limit: int = 8) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT target_url, domain, last_output_root, last_snapshot, last_site_type,
                       last_estimated_files, last_estimated_size, updated_at
                FROM projects
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_project(self, target_url: str, purge_related: bool = True) -> Dict[str, int]:
        target_url = self._normalize_target_url(target_url)
        domain = self._extract_domain(target_url)
        variants = self._target_url_variants(target_url)
        removed = {
            "projects": 0,
            "inspect_cache": 0,
            "analyze_cache": 0,
            "sitemap_cache": 0,
            "jobs_history": 0,
        }

        def _delete_by_target(conn: sqlite3.Connection, table: str) -> int:
            if not variants:
                return 0
            clauses: List[str] = []
            params: List[str] = []
            for value in variants:
                clauses.append("target_url = ?")
                params.append(value)
                clauses.append("target_url LIKE ?")
                params.append(value + "/%")
            where_sql = " OR ".join(clauses)
            cur = conn.execute(f"DELETE FROM {table} WHERE {where_sql}", tuple(params))
            return max(0, int(cur.rowcount or 0))

        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM projects WHERE target_url = ? OR domain = ?", (target_url, domain))
            removed["projects"] = max(0, int(cur.rowcount or 0))
            if purge_related:
                removed["inspect_cache"] = _delete_by_target(conn, "inspect_cache")
                removed["analyze_cache"] = _delete_by_target(conn, "analyze_cache")
                removed["sitemap_cache"] = _delete_by_target(conn, "sitemap_cache")
                removed["jobs_history"] = _delete_by_target(conn, "jobs_history")
        return removed

    def list_project_output_roots(self, target_url: str) -> List[str]:
        target_url = self._normalize_target_url(target_url)
        domain = self._extract_domain(target_url)
        variants = self._target_url_variants(target_url)

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT COALESCE(last_output_root, '') AS last_output_root
                FROM projects
                WHERE domain = ? OR target_url = ?
                """,
                (domain, target_url),
            ).fetchall()

        out: List[str] = []
        for row in rows:
            value = str(row["last_output_root"] or "").strip()
            if value:
                out.append(value)

        # keep unique order
        seen: set[str] = set()
        unique: List[str] = []
        for value in out:
            if value in seen:
                continue
            seen.add(value)
            unique.append(value)
        return unique

    def list_recent_jobs(self, limit: int = 12) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT job_type, target_url, snapshot, state, summary_json, created_at
                FROM jobs_history
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()

        out: List[Dict[str, Any]] = []
        now = int(time.time())
        for row in rows:
            item = dict(row)
            try:
                item["summary"] = json.loads(item.get("summary_json") or "{}")
            except json.JSONDecodeError:
                item["summary"] = {}
            created_at = int(item.get("created_at") or 0)
            item["age_seconds"] = max(0, now - created_at)
            item["created_local"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_at)) if created_at else ""
            out.append(item)
        return out

    def add_job_history(
        self,
        job_type: str,
        target_url: str,
        state: str,
        snapshot: Optional[str] = None,
        summary: Optional[Dict[str, Any]] = None,
    ) -> None:
        target_url = self._normalize_target_url(target_url)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs_history(job_type,target_url,snapshot,state,summary_json,created_at)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    job_type,
                    target_url,
                    snapshot,
                    state,
                    json.dumps(summary or {}),
                    int(time.time()),
                ),
            )

    def get_project_data_status(self, target_url: str) -> Dict[str, Any]:
        target_url = self._normalize_target_url(target_url)
        with self._connect() as conn:
            project_row = conn.execute(
                """
                SELECT target_url, domain, last_snapshot, last_site_type, last_output_root, updated_at
                FROM projects
                WHERE target_url = ?
                LIMIT 1
                """,
                (target_url,),
            ).fetchone()

            inspect_row = conn.execute(
                """
                SELECT payload_json, created_at
                FROM inspect_cache
                WHERE target_url = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (target_url,),
            ).fetchone()

            analyze_rows = conn.execute(
                """
                SELECT snapshot, MAX(created_at) AS created_at
                FROM analyze_cache
                WHERE target_url = ? AND COALESCE(snapshot, '') <> ''
                GROUP BY snapshot
                ORDER BY created_at DESC
                LIMIT 60
                """,
                (target_url,),
            ).fetchall()

            sitemap_rows = conn.execute(
                """
                SELECT snapshot, MAX(created_at) AS created_at
                FROM sitemap_cache
                WHERE target_url = ? AND COALESCE(snapshot, '') <> ''
                GROUP BY snapshot
                ORDER BY created_at DESC
                LIMIT 60
                """,
                (target_url,),
            ).fetchall()

        now = int(time.time())

        project: Dict[str, Any] = {
            "exists": project_row is not None,
            "target_url": target_url,
            "domain": "",
            "last_snapshot": "",
            "last_site_type": "",
            "last_output_root": "",
            "updated_at": 0,
            "updated_local": "",
        }
        if project_row is not None:
            project = {
                "exists": True,
                "target_url": str(project_row["target_url"] or target_url),
                "domain": str(project_row["domain"] or ""),
                "last_snapshot": str(project_row["last_snapshot"] or ""),
                "last_site_type": str(project_row["last_site_type"] or ""),
                "last_output_root": str(project_row["last_output_root"] or ""),
                "updated_at": int(project_row["updated_at"] or 0),
                "updated_local": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(project_row["updated_at"] or 0))) if int(project_row["updated_at"] or 0) else "",
            }

        inspect_payload: Dict[str, Any] = {}
        inspect_created_at = 0
        if inspect_row is not None:
            inspect_created_at = int(inspect_row["created_at"] or 0)
            try:
                inspect_payload = json.loads(inspect_row["payload_json"] or "{}")
            except json.JSONDecodeError:
                inspect_payload = {}

        inspect = {
            "has_data": bool(inspect_payload),
            "created_at": inspect_created_at,
            "created_local": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(inspect_created_at)) if inspect_created_at else "",
            "age_seconds": max(0, now - inspect_created_at) if inspect_created_at else 0,
            "total_snapshots": int(inspect_payload.get("total_snapshots", 0) or 0),
            "total_ok_snapshots": int(inspect_payload.get("total_ok_snapshots", 0) or 0),
            "first_found_snapshot": str(inspect_payload.get("first_snapshot") or ""),
            "latest_found_snapshot": str(inspect_payload.get("latest_snapshot") or ""),
            "latest_ok_snapshot": str(inspect_payload.get("latest_ok_snapshot") or ""),
        }

        def _rows_to_snapshot_list(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            for row in rows:
                created_at = int(row["created_at"] or 0)
                out.append(
                    {
                        "snapshot": str(row["snapshot"] or ""),
                        "created_at": created_at,
                        "created_local": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_at)) if created_at else "",
                        "age_seconds": max(0, now - created_at) if created_at else 0,
                    }
                )
            return out

        analyze_snaps = _rows_to_snapshot_list(analyze_rows)
        sitemap_snaps = _rows_to_snapshot_list(sitemap_rows)

        return {
            "target_url": target_url,
            "project": project,
            "inspect": inspect,
            "analyze": {
                "count": len(analyze_snaps),
                "snapshots": analyze_snaps,
            },
            "sitemap": {
                "count": len(sitemap_snaps),
                "snapshots": sitemap_snaps,
            },
        }

    def prune_old_data(self, cache_retention_seconds: int, jobs_retention_seconds: int) -> Dict[str, int]:
        now = int(time.time())
        cache_cutoff = now - max(60, int(cache_retention_seconds))
        jobs_cutoff = now - max(60, int(jobs_retention_seconds))
        removed = {
            "inspect_cache": 0,
            "analyze_cache": 0,
            "sitemap_cache": 0,
            "jobs_history": 0,
        }
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM inspect_cache WHERE created_at < ?", (cache_cutoff,))
            removed["inspect_cache"] = max(0, int(cur.rowcount or 0))
            cur = conn.execute("DELETE FROM analyze_cache WHERE created_at < ?", (cache_cutoff,))
            removed["analyze_cache"] = max(0, int(cur.rowcount or 0))
            cur = conn.execute("DELETE FROM sitemap_cache WHERE created_at < ?", (cache_cutoff,))
            removed["sitemap_cache"] = max(0, int(cur.rowcount or 0))
            cur = conn.execute("DELETE FROM jobs_history WHERE created_at < ?", (jobs_cutoff,))
            removed["jobs_history"] = max(0, int(cur.rowcount or 0))
        return removed

    def _get_cache_row_with_meta(self, table: str, cache_key: str, max_age_seconds: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT cache_key, payload_json, created_at FROM {table} WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        return self._decode_cache_row(row, max_age_seconds)

    def _decode_cache_row(self, row: Optional[sqlite3.Row], max_age_seconds: int) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        age_seconds = int(time.time()) - int(row["created_at"])
        if age_seconds > max_age_seconds:
            return None
        try:
            payload = json.loads(row["payload_json"])
            return {
                "cache_key": row["cache_key"],
                "payload": payload,
                "created_at": int(row["created_at"]),
                "age_seconds": max(0, age_seconds),
            }
        except json.JSONDecodeError:
            return None
