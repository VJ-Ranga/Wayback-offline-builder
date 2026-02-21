from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


CDX_API = "https://web.archive.org/cdx/search/cdx"
WAYBACK_RAW = "https://web.archive.org/web/{timestamp}id_/{url}"
WAYBACK_AVAILABLE = "https://archive.org/wayback/available"
ATTRS_TO_SCAN = ("src", "href", "poster", "data-src", "data-href")
CSS_URL_RE = re.compile(r"url\(([^)]+)\)", re.IGNORECASE)
BAD_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "#")
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class FileRecord:
    url: str
    local_path: str
    mime: str
    timestamp: str


@dataclass
class ArchiveResult:
    target_url: str
    latest_snapshot: str
    total_snapshots: int
    output_dir: str
    files_downloaded: int
    files_recovered: int
    missing_urls: List[str]
    files: List[FileRecord]
    expected_sample_files: int
    expected_sample_size_bytes: int
    coverage_percent: float
    missing_expected_urls: List[str]
    seconds: float


class ArchiveWebTool:
    def __init__(self, timeout: int = 45) -> None:
        self.session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.timeout = timeout
        self._cdx_cache: Dict[str, List[str]] = {}
        self._archive_unavailable_until = 0.0

    def _mark_archive_unavailable(self, hold_seconds: int = 120) -> None:
        self._archive_unavailable_until = max(self._archive_unavailable_until, time.time() + max(30, hold_seconds))

    def _archive_unavailable_recent(self) -> bool:
        return time.time() < self._archive_unavailable_until

    def _status_code_from_exception(self, exc: requests.RequestException) -> Optional[int]:
        response = getattr(exc, "response", None)
        if response is None:
            return None
        try:
            return int(response.status_code)
        except Exception:
            return None

    def _get_with_backoff(
        self,
        url: str,
        *,
        params: Optional[Dict[str, str]] = None,
        timeout: Tuple[int, int] = (10, 30),
        retries: int = 2,
    ) -> requests.Response:
        last_exc: Optional[requests.RequestException] = None
        for attempt in range(max(0, retries) + 1):
            try:
                response = self.session.get(url, params=params, timeout=timeout)
                if int(response.status_code) == 503:
                    self._mark_archive_unavailable()
                    if attempt < retries:
                        time.sleep(0.6 * (2 ** attempt))
                        continue
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_exc = exc
                if self._status_code_from_exception(exc) == 503:
                    self._mark_archive_unavailable()
                if attempt < retries:
                    time.sleep(0.6 * (2 ** attempt))
                    continue
                break

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Archive request failed")

    def inspect(
        self,
        target_url: str,
        progress_callback: Optional[Callable[[Dict[str, object]], None]] = None,
        display_limit: int = 120,
        cdx_limit: int = 20000,
        wait_if_paused: Optional[Callable[[str], None]] = None,
        should_abort: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, object]:
        normalized = self._normalize_target(target_url)
        merged = self._merge_variant_snapshots(
            normalized,
            progress_callback=progress_callback,
            cdx_limit=cdx_limit,
            wait_if_paused=wait_if_paused,
            should_abort=should_abort,
        )
        all_snapshots = merged["all"]
        ok_snapshots = merged["ok"]

        inspected_scope = normalized
        if not all_snapshots:
            root_url = self._root_url(normalized)
            if root_url != normalized:
                merged_root = self._merge_variant_snapshots(
                    root_url,
                    progress_callback=progress_callback,
                    cdx_limit=cdx_limit,
                    wait_if_paused=wait_if_paused,
                    should_abort=should_abort,
                )
                if merged_root["all"]:
                    merged = merged_root
                    all_snapshots = merged_root["all"]
                    ok_snapshots = merged_root["ok"]
                    inspected_scope = root_url

        if not all_snapshots:
            fallback_list = self._fallback_variant_timestamps(normalized)
            if fallback_list:
                all_snapshots = sorted(set(fallback_list))
                ok_snapshots = sorted(set(fallback_list))
                merged["variants"] = merged.get("variants", [])
                merged["fallback_used"] = True
            else:
                if self._archive_unavailable_recent():
                    raise RuntimeError(
                        "Archive.org is temporarily unavailable (503). Please retry in a few minutes or use cached local data."
                    )
                if int(merged.get("failed_variants", 0)) >= int(merged.get("variant_count", 0)):
                    raise RuntimeError("Wayback did not respond in time. Try lower search depth first.")
                raise RuntimeError("No archived snapshots found for this URL")
        total_all = len(all_snapshots)
        total_ok = len(ok_snapshots)
        if total_all < total_ok:
            total_all = total_ok
        return {
            "target_url": normalized,
            "inspected_scope": inspected_scope,
            "total_snapshots": total_all,
            "total_ok_snapshots": total_ok,
            "latest_snapshot": all_snapshots[-1],
            "latest_ok_snapshot": ok_snapshots[-1] if ok_snapshots else all_snapshots[-1],
            "first_snapshot": all_snapshots[0],
            "snapshots": list(reversed(all_snapshots[-max(5, display_limit):])),
            "calendar": self._build_calendar(all_snapshots),
            "variants": merged["variants"],
            "display_limit": max(5, display_limit),
            "cdx_limit": max(500, cdx_limit),
            "limited_mode": True,
            "fallback_used": bool(merged.get("fallback_used", False)),
        }

    def analyze(
        self,
        target_url: str,
        selected_snapshot: Optional[str] = None,
        cdx_limit: int = 15000,
        progress_callback: Optional[Callable[[Dict[str, object]], None]] = None,
        wait_if_paused: Optional[Callable[[str], None]] = None,
        should_abort: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, object]:
        normalized = self._normalize_target(target_url)
        merged = self._merge_variant_snapshots(normalized)
        snapshots = merged["all"]
        if not snapshots:
            fallback_ts = self._fallback_latest_timestamp(normalized)
            if not fallback_ts:
                root_url = self._root_url(normalized)
                if root_url != normalized:
                    fallback_ts = self._fallback_latest_timestamp(root_url)
            if fallback_ts:
                snapshots = [fallback_ts]
                merged["fallback_used"] = True
            else:
                if self._archive_unavailable_recent():
                    raise RuntimeError(
                        "Archive.org is temporarily unavailable (503). Please retry in a few minutes or use cached local data."
                    )
                raise RuntimeError("No archived snapshots found for this URL")

        chosen = selected_snapshot.strip() if selected_snapshot else snapshots[-1]
        if chosen not in snapshots:
            chosen = snapshots[-1]

        wildcards = [self._wildcard_url(v["url"]) for v in merged["variants"]]
        self._emit_progress(progress_callback, stage="prepare", message="Preparing analysis", percent=8)
        unique_rows = self._collect_cdx_rows(
            wildcards,
            to_timestamp=chosen,
            limit=max(1000, cdx_limit),
            wait_if_paused=wait_if_paused,
            should_abort=should_abort,
            progress_callback=progress_callback,
            progress_stage="inventory",
        )
        file_count = len(unique_rows)
        total_size = 0
        mime_buckets: Dict[str, int] = {}
        ext_buckets: Dict[str, int] = {}
        signals: List[str] = []
        folder_counts: Dict[str, int] = {}
        page_candidates: List[str] = []

        wp_themes: set[str] = set()
        wp_plugins: set[str] = set()
        wp_categories: set[str] = set()
        wp_tags: set[str] = set()
        wp_post_types: set[str] = set()
        wp_blog_posts: List[str] = []
        wp_json_routes: set[str] = set()

        for idx, row in enumerate(unique_rows, start=1):
            if should_abort is not None and should_abort():
                raise RuntimeError("Stopped by user")
            if wait_if_paused is not None and idx % 20 == 0:
                wait_if_paused("analyze")
            if idx % 50 == 0:
                self._emit_progress(
                    progress_callback,
                    stage="analyze",
                    message="Analyzing discovered files",
                    percent=min(96, 25 + int((idx / max(len(unique_rows), 1)) * 65)),
                    processed=idx,
                    total=len(unique_rows),
                )
            if len(row) < 5:
                continue
            original = row[1]
            mime = (row[2] or "unknown").lower()
            length = row[3]
            parsed = urlparse(original)
            path = parsed.path or "/"
            lower_path = path.lower()

            mime_buckets[mime] = mime_buckets.get(mime, 0) + 1
            ext = self._extension_of_url(original)
            ext_buckets[ext] = ext_buckets.get(ext, 0) + 1

            if str(length).isdigit():
                total_size += int(length)

            lower_url = original.lower()
            if "/wp-content/" in lower_url or "/wp-includes/" in lower_url or "/wp-json/" in lower_url:
                signals.append("wordpress")
            if "wixstatic.com" in lower_url or "parastorage.com" in lower_url:
                signals.append("wix")
            if "cdn.shopify.com" in lower_url or "shopify" in lower_url:
                signals.append("shopify")
            if lower_url.endswith(".php"):
                signals.append("php")

            folder = self._folder_of_path(path)
            folder_counts[folder] = folder_counts.get(folder, 0) + 1
            if self._looks_like_page(path, mime):
                page_candidates.append(path)

            theme = self._extract_wp_slug(lower_path, "/wp-content/themes/")
            plugin = self._extract_wp_slug(lower_path, "/wp-content/plugins/")
            if theme:
                wp_themes.add(theme)
            if plugin:
                wp_plugins.add(plugin)

            category = self._extract_wp_slug(lower_path, "/category/")
            tag = self._extract_wp_slug(lower_path, "/tag/")
            if category:
                wp_categories.add(unquote(category))
            if tag:
                wp_tags.add(unquote(tag))

            route = self._extract_wp_json_route(lower_path)
            if route:
                wp_json_routes.add(route)
                parts = route.split("/")
                if len(parts) >= 3 and parts[0] == "wp" and parts[1] == "v2":
                    wp_post_types.add(parts[2])

            if re.search(r"/\d{4}/\d{2}/[^/]+/?$", lower_path):
                wp_blog_posts.append(path)

        html = self._download_at_timestamp(normalized, chosen)
        if html and ("text/html" in html[1] or "application/xhtml" in html[1]):
            body = self._decode_text(html[0]).lower()
            if "wp-content" in body or "wordpress" in body:
                signals.append("wordpress")
            if "wix" in body or "wixstatic" in body:
                signals.append("wix")
            if "shopify" in body:
                signals.append("shopify")
            if "<div id=\"root\"" in body or "__next" in body:
                signals.append("spa")

        kind = self._guess_site_type(signals)
        top_mimes = sorted(mime_buckets.items(), key=lambda x: x[1], reverse=True)[:8]
        top_exts = sorted(ext_buckets.items(), key=lambda x: x[1], reverse=True)[:8]
        top_folders = sorted(folder_counts.items(), key=lambda x: x[1], reverse=True)[:25]
        site_pages = sorted(set(page_candidates))[:200]
        wp_blog_posts = sorted(set(wp_blog_posts))[:80]

        self._emit_progress(progress_callback, stage="done", message="Analysis complete", percent=100)
        return {
            "target_url": normalized,
            "selected_snapshot": chosen,
            "estimated_files": file_count,
            "estimated_size_bytes": total_size,
            "estimated_size_human": self._human_size(total_size),
            "site_type": kind,
            "top_mime_types": top_mimes,
            "top_extensions": top_exts,
            "top_folders": top_folders,
            "site_pages": site_pages,
            "variants_checked": merged["variants"],
            "wordpress": {
                "detected": kind == "WordPress" or bool(wp_themes or wp_plugins or wp_json_routes),
                "themes": sorted(wp_themes),
                "plugins": sorted(wp_plugins),
                "categories": sorted(wp_categories),
                "tags": sorted(wp_tags),
                "post_types": sorted(wp_post_types),
                "blog_posts": wp_blog_posts,
                "wp_json_routes": sorted(wp_json_routes)[:120],
            },
        }

    def audit(
        self,
        target_url: str,
        output_root: str,
        selected_snapshot: Optional[str] = None,
        progress_callback: Optional[Callable[[Dict[str, object]], None]] = None,
        wait_if_paused: Optional[Callable[[str], None]] = None,
        should_abort: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, object]:
        normalized = self._normalize_target(target_url)
        merged = self._merge_variant_snapshots(normalized)
        ok_snapshots = merged["ok"]
        if not ok_snapshots:
            raise RuntimeError("No archived snapshots found for this URL")

        chosen = selected_snapshot.strip() if selected_snapshot else ok_snapshots[-1]
        if chosen not in ok_snapshots:
            chosen = ok_snapshots[-1]

        host_slug = self._safe_name(urlparse(normalized).netloc)
        output_dir = self._resolve_output_dir(Path(output_root), host_slug, chosen)
        manifest_path = output_dir / "manifest.json"
        if not manifest_path.exists():
            raise RuntimeError(f"Manifest not found: {manifest_path}")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest.get("files", [])
        downloaded_urls = {
            self._clean_url(item.get("url", ""))
            for item in files
            if isinstance(item, dict) and item.get("url")
        }

        allowed_hosts = {urlparse(v["url"]).netloc for v in merged["variants"]}
        wildcards = [self._wildcard_url(v["url"]) for v in merged["variants"]]
        self._emit_progress(progress_callback, stage="prepare", message="Loading audit inventory", percent=10)
        rows = self._collect_cdx_rows(
            wildcards,
            to_timestamp=chosen,
            limit=60000,
            wait_if_paused=wait_if_paused,
            should_abort=should_abort,
            progress_callback=progress_callback,
            progress_stage="inventory",
        )
        expected_urls = {
            self._clean_url(row[1])
            for row in rows
            if len(row) >= 5 and urlparse(row[1]).netloc in allowed_hosts
        }

        have_urls = sorted(downloaded_urls.intersection(expected_urls))
        missing_urls = sorted(expected_urls.difference(downloaded_urls))
        extra_urls = sorted(downloaded_urls.difference(expected_urls))
        coverage = round((len(have_urls) / len(expected_urls)) * 100, 2) if expected_urls else 0.0

        downloaded_bytes = 0
        for idx, item in enumerate(files, start=1):
            if should_abort is not None and should_abort():
                raise RuntimeError("Stopped by user")
            if wait_if_paused is not None and idx % 20 == 0:
                wait_if_paused("audit")
            if not isinstance(item, dict):
                continue
            local = item.get("local_path")
            if not local:
                continue
            full = output_dir / local
            if full.exists() and full.is_file():
                downloaded_bytes += full.stat().st_size

        self._emit_progress(progress_callback, stage="done", message="Check complete", percent=100)
        return {
            "target_url": normalized,
            "snapshot": chosen,
            "output_dir": str(output_dir),
            "expected_count": len(expected_urls),
            "downloaded_count": len(downloaded_urls),
            "have_count": len(have_urls),
            "missing_count": len(missing_urls),
            "extra_count": len(extra_urls),
            "coverage_percent": coverage,
            "downloaded_size_bytes": downloaded_bytes,
            "downloaded_size_human": self._human_size(downloaded_bytes),
            "have_urls": have_urls[:300],
            "missing_urls": missing_urls[:500],
            "extra_urls": extra_urls[:200],
        }

    def download_missing(
        self,
        target_url: str,
        output_root: str,
        selected_snapshot: Optional[str] = None,
        limit: int = 300,
        progress_callback: Optional[Callable[[Dict[str, object]], None]] = None,
        skip_errors: bool = True,
        should_abort: Optional[Callable[[], bool]] = None,
        wait_if_paused: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, object]:
        started = time.time()
        normalized = self._normalize_target(target_url)

        def _variant_progress(payload: Dict[str, object]) -> None:
            self._emit_progress(
                progress_callback,
                stage="variant",
                message="Checking archive variants for missing recovery",
                percent=max(2, min(25, int(payload.get("percent", 0) * 0.25))),
                attempted=0,
                total=limit,
                added=0,
                failed=0,
                bytes_added=0,
                current_url="",
                phase_detail=str(payload.get("message", "")),
                current_variant=str(payload.get("current_variant", "")),
                variants_done=int(payload.get("variants_done", 0)),
                variants_total=int(payload.get("variants_total", 0)),
                total_expected=int(payload.get("total_captures", 0)),
                missing_found=0,
            )

        merged = self._merge_variant_snapshots(normalized, progress_callback=_variant_progress)
        ok_snapshots = merged["ok"]
        if not ok_snapshots:
            raise RuntimeError("No archived snapshots found for this URL")

        chosen = selected_snapshot.strip() if selected_snapshot else ok_snapshots[-1]
        if chosen not in ok_snapshots:
            chosen = ok_snapshots[-1]

        host_slug = self._safe_name(urlparse(normalized).netloc)
        output_dir = self._resolve_output_dir(Path(output_root), host_slug, chosen)
        manifest_path = output_dir / "manifest.json"
        if not manifest_path.exists():
            raise RuntimeError(f"Manifest not found: {manifest_path}")

        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_files = payload.get("files", []) if isinstance(payload, dict) else []
        if not isinstance(manifest_files, list):
            manifest_files = []

        existing_map: Dict[str, Dict[str, object]] = {}
        for item in manifest_files:
            if isinstance(item, dict) and item.get("url"):
                existing_map[self._clean_url(str(item["url"]))] = item

        self._emit_progress(
            progress_callback,
            stage="prepare",
            message="Loading manifest and downloaded files list",
            percent=30,
            attempted=0,
            total=limit,
            added=0,
            failed=0,
            bytes_added=0,
            current_url="",
            phase_detail="Manifest parsed",
            current_variant="",
            variants_done=len(merged["variants"]),
            variants_total=len(merged["variants"]),
            total_expected=0,
            missing_found=0,
        )

        allowed_hosts = {urlparse(v["url"]).netloc for v in merged["variants"]}
        wildcards = [self._wildcard_url(v["url"]) for v in merged["variants"]]

        self._emit_progress(
            progress_callback,
            stage="inventory",
            message="Scanning archive index for expected files",
            percent=42,
            attempted=0,
            total=limit,
            added=0,
            failed=0,
            bytes_added=0,
            current_url="",
            phase_detail="Querying CDX inventory (this can be slow)",
            current_variant="",
            variants_done=len(merged["variants"]),
            variants_total=len(merged["variants"]),
            total_expected=0,
            missing_found=0,
        )

        rows = self._collect_cdx_rows(wildcards, to_timestamp=chosen, limit=70000)
        expected_urls = [
            self._clean_url(row[1])
            for row in rows
            if len(row) >= 5 and urlparse(row[1]).netloc in allowed_hosts
        ]
        missing_urls = [u for u in dict.fromkeys(expected_urls) if u not in existing_map]
        targets = missing_urls[: max(1, limit)]

        added = 0
        failed = 0
        recovered = 0
        bytes_added = 0

        self._emit_progress(
            progress_callback,
            stage="plan",
            message="Missing file plan ready",
            percent=55,
            attempted=0,
            total=len(targets),
            added=0,
            failed=0,
            bytes_added=0,
            current_url="",
            phase_detail="Starting download of missing files",
            current_variant="",
            variants_done=len(merged["variants"]),
            variants_total=len(merged["variants"]),
            total_expected=len(expected_urls),
            missing_found=len(missing_urls),
        )

        for idx, url in enumerate(targets, start=1):
            if wait_if_paused is not None:
                wait_if_paused(url)
            if should_abort is not None and should_abort():
                raise RuntimeError("Stopped by user")
            self._emit_progress(
                progress_callback,
                stage="download",
                message="Downloading missing files",
                percent=min(98, int((idx / max(len(targets), 1)) * 100)),
                attempted=idx - 1,
                total=len(targets),
                added=added,
                failed=failed,
                bytes_added=bytes_added,
                current_url=url,
                phase_detail="Downloading and repairing file",
                current_variant="",
                variants_done=len(merged["variants"]),
                variants_total=len(merged["variants"]),
                total_expected=len(expected_urls),
                missing_found=len(missing_urls),
            )
            try:
                download = self._download_with_repair(url, chosen)
                if not download:
                    failed += 1
                    continue

                body, mime, used_timestamp = download
                local_abs, local_rel = self._save_file(output_dir, url, body, mime)
                _ = local_abs
                existing_map[url] = {
                    "url": url,
                    "local_path": local_rel,
                    "mime": mime,
                    "timestamp": used_timestamp,
                }
                added += 1
                bytes_added += len(body)
                if used_timestamp != chosen:
                    recovered += 1
            except Exception as exc:
                failed += 1
                self._emit_progress(
                    progress_callback,
                    stage="download",
                    message="Error on file, skipping to next",
                    percent=min(98, int((idx / max(len(targets), 1)) * 100)),
                    attempted=idx,
                    total=len(targets),
                    added=added,
                    failed=failed,
                    bytes_added=bytes_added,
                    current_url=url,
                    last_error=str(exc),
                    phase_detail="File failed, skipped",
                    current_variant="",
                    variants_done=len(merged["variants"]),
                    variants_total=len(merged["variants"]),
                    total_expected=len(expected_urls),
                    missing_found=len(missing_urls),
                )
                if not skip_errors:
                    raise
                continue

        payload["files"] = list(existing_map.values())
        payload["files_downloaded"] = len(payload["files"])
        payload["files_recovered"] = int(payload.get("files_recovered", 0)) + recovered
        payload["last_missing_repair"] = {
            "snapshot": chosen,
            "attempted": len(targets),
            "added": added,
            "failed": failed,
            "recovered": recovered,
            "bytes_added": bytes_added,
            "seconds": round(time.time() - started, 2),
        }
        manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        self._emit_progress(
            progress_callback,
            stage="done",
            message="Missing files download completed",
            percent=100,
            attempted=len(targets),
            total=len(targets),
            added=added,
            failed=failed,
            bytes_added=bytes_added,
            current_url="",
            phase_detail="Missing recovery completed",
            current_variant="",
            variants_done=len(merged["variants"]),
            variants_total=len(merged["variants"]),
            total_expected=len(expected_urls),
            missing_found=len(missing_urls),
        )

        return {
            "snapshot": chosen,
            "output_dir": str(output_dir),
            "attempted": len(targets),
            "added": added,
            "failed": failed,
            "recovered": recovered,
            "bytes_added": bytes_added,
            "bytes_added_human": self._human_size(bytes_added),
            "seconds": round(time.time() - started, 2),
        }

    def _resolve_output_dir(self, output_root: Path, host_slug: str, chosen: str) -> Path:
        direct = output_root / f"{host_slug}_{chosen}"
        if (direct / "manifest.json").exists():
            return direct

        if (output_root / "manifest.json").exists():
            return output_root

        if output_root.name.startswith(f"{host_slug}_"):
            parent_candidate = output_root.parent / f"{host_slug}_{chosen}"
            if (parent_candidate / "manifest.json").exists():
                return parent_candidate

        matches = sorted(output_root.glob(f"{host_slug}_*/manifest.json"))
        if matches:
            exact = [m for m in matches if m.parent.name == f"{host_slug}_{chosen}"]
            if exact:
                return exact[-1].parent
            return matches[-1].parent

        return direct

    def run(
        self,
        target_url: str,
        output_root: str,
        max_files: int = 400,
        preferred_snapshot: Optional[str] = None,
        progress_callback: Optional[Callable[[Dict[str, object]], None]] = None,
        wait_if_paused: Optional[Callable[[str], None]] = None,
    ) -> ArchiveResult:
        started = time.time()
        normalized = self._normalize_target(target_url)
        merged = self._merge_variant_snapshots(normalized)
        snapshots = merged["ok"]
        if not snapshots:
            raise RuntimeError("No archived snapshots found for this URL")
        allowed_hosts = {urlparse(v["url"]).netloc for v in merged["variants"]}

        latest = snapshots[-1]
        if preferred_snapshot and preferred_snapshot.isdigit() and len(preferred_snapshot) == 14:
            latest = preferred_snapshot

        wildcards = [self._wildcard_url(v["url"]) for v in merged["variants"]]
        inventory_rows = self._collect_cdx_rows(wildcards, to_timestamp=latest, limit=max(5000, max_files * 8))
        inventory_urls = [
            row[1]
            for row in inventory_rows
            if len(row) >= 5 and urlparse(row[1]).netloc in allowed_hosts
        ]
        prioritized_inventory = self._prioritize_inventory_urls(inventory_rows, allowed_hosts)
        inventory_size = sum(int(r[3]) for r in inventory_rows if len(r) >= 4 and str(r[3]).isdigit())

        host_slug = self._safe_name(urlparse(normalized).netloc)
        output_dir = Path(output_root) / f"{host_slug}_{latest}"
        output_dir.mkdir(parents=True, exist_ok=True)

        seed_urls = [normalized] + prioritized_inventory[: max(50, min(max_files * 2, 2000))]
        queue: deque[str] = deque(list(dict.fromkeys(seed_urls)))
        seen: set[str] = set()
        files: Dict[str, FileRecord] = {}
        missing: List[str] = []
        total_bytes = 0

        self._emit_progress(
            progress_callback,
            stage="prepare",
            message="Starting download...",
            percent=1,
            files_downloaded=0,
            max_files=max_files,
            bytes_downloaded=0,
            recovered_files=0,
            current_url=normalized,
            queue_size=len(queue),
        )

        while queue and len(files) < max_files:
            current_url = queue.popleft()
            current_url = self._clean_url(current_url)
            if wait_if_paused is not None:
                wait_if_paused(current_url)
            if current_url in seen:
                continue
            seen.add(current_url)

            estimated_percent = min(96, int((len(files) / max_files) * 100))
            self._emit_progress(
                progress_callback,
                stage="download",
                message="Downloading archived files",
                percent=estimated_percent,
                files_downloaded=len(files),
                max_files=max_files,
                bytes_downloaded=total_bytes,
                recovered_files=sum(1 for r in files.values() if r.timestamp != latest),
                current_url=current_url,
                queue_size=len(queue),
            )

            download = self._download_with_repair(current_url, latest)
            if not download:
                missing.append(current_url)
                continue

            body, mime, used_timestamp = download
            local_abs, local_rel = self._save_file(output_dir, current_url, body, mime)
            total_bytes += len(body)
            files[current_url] = FileRecord(
                url=current_url,
                local_path=local_rel,
                mime=mime,
                timestamp=used_timestamp,
            )

            discovered = self._discover_links(current_url, body, mime)
            for link in discovered:
                if len(files) + len(queue) >= max_files:
                    break
                if self._is_allowed_host(allowed_hosts, link) and link not in seen:
                    queue.append(link)

        if len(files) < max_files:
            for candidate in prioritized_inventory:
                if len(files) >= max_files:
                    break
                candidate = self._clean_url(candidate)
                if candidate in seen:
                    continue
                seen.add(candidate)
                download = self._download_with_repair(candidate, latest)
                if not download:
                    missing.append(candidate)
                    continue
                body, mime, used_timestamp = download
                local_abs, local_rel = self._save_file(output_dir, candidate, body, mime)
                total_bytes += len(body)
                files[candidate] = FileRecord(
                    url=candidate,
                    local_path=local_rel,
                    mime=mime,
                    timestamp=used_timestamp,
                )

        self._emit_progress(
            progress_callback,
            stage="rewrite",
            message="Rewriting links for offline use",
            percent=97,
            files_downloaded=len(files),
            max_files=max_files,
            bytes_downloaded=total_bytes,
            recovered_files=sum(1 for r in files.values() if r.timestamp != latest),
            current_url="",
            queue_size=0,
        )

        for original_url, record in files.items():
            if wait_if_paused is not None:
                wait_if_paused(original_url)
            full_path = Path(output_dir) / record.local_path
            self._rewrite_for_offline(
                output_dir=Path(output_dir),
                file_path=full_path,
                page_url=original_url,
                mime=record.mime,
                url_to_local={u: r.local_path for u, r in files.items()},
            )

        recovered = sum(1 for r in files.values() if r.timestamp != latest)
        inventory_unique = set(inventory_urls)
        downloaded_urls = set(files.keys())
        covered = len(downloaded_urls.intersection(inventory_unique))
        coverage = round((covered / len(inventory_unique)) * 100, 2) if inventory_unique else 0.0
        missing_expected = sorted(inventory_unique.difference(downloaded_urls))[:300]
        result = ArchiveResult(
            target_url=normalized,
            latest_snapshot=latest,
            total_snapshots=len(snapshots),
            output_dir=str(output_dir),
            files_downloaded=len(files),
            files_recovered=recovered,
            missing_urls=missing,
            files=list(files.values()),
            expected_sample_files=len(inventory_unique),
            expected_sample_size_bytes=inventory_size,
            coverage_percent=coverage,
            missing_expected_urls=missing_expected,
            seconds=round(time.time() - started, 2),
        )
        self._write_manifest(output_dir, result)

        self._emit_progress(
            progress_callback,
            stage="done",
            message="Download finished",
            percent=100,
            files_downloaded=len(files),
            max_files=max_files,
            bytes_downloaded=total_bytes,
            recovered_files=recovered,
            current_url="",
            queue_size=0,
        )

        return result

    def _emit_progress(self, callback: Optional[Callable[[Dict[str, object]], None]], **payload: object) -> None:
        if callback is None:
            return
        callback(payload)

    def _write_manifest(self, output_dir: Path, result: ArchiveResult) -> None:
        payload = {
            "target_url": result.target_url,
            "latest_snapshot": result.latest_snapshot,
            "total_snapshots": result.total_snapshots,
            "output_dir": result.output_dir,
            "files_downloaded": result.files_downloaded,
            "files_recovered": result.files_recovered,
            "expected_sample_files": result.expected_sample_files,
            "expected_sample_size_bytes": result.expected_sample_size_bytes,
            "coverage_percent": result.coverage_percent,
            "missing_count": len(result.missing_urls),
            "missing_urls": result.missing_urls,
            "missing_expected_count": len(result.missing_expected_urls),
            "missing_expected_urls": result.missing_expected_urls,
            "seconds": result.seconds,
            "files": [
                {
                    "url": f.url,
                    "local_path": f.local_path,
                    "mime": f.mime,
                    "timestamp": f.timestamp,
                }
                for f in result.files
            ],
        }
        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _list_snapshots(
        self,
        target_url: str,
        success_only: bool = True,
        max_rows: Optional[int] = None,
        strict: bool = True,
    ) -> List[str]:
        params = {
            "url": target_url,
            "output": "json",
            "fl": "timestamp",
            "limit": str(max_rows if max_rows is not None else 200000),
        }
        if success_only:
            params["filter"] = "statuscode:200"
        try:
            response = self._get_with_backoff(
                CDX_API,
                params=params,
                timeout=(8, min(self.timeout, 25)),
                retries=2,
            )
            rows = response.json()
        except requests.RequestException as exc:
            if not strict:
                return []
            if self._status_code_from_exception(exc) == 503 or self._archive_unavailable_recent():
                raise RuntimeError(
                    "Archive.org is temporarily unavailable (503). Try again in a few minutes or use local cache."
                ) from exc
            raise RuntimeError(
                "Wayback API timed out while listing snapshots. Try again or reduce site scope."
            ) from exc

        if len(rows) <= 1:
            return []
        timestamps = sorted({row[0] for row in rows[1:] if row and row[0].isdigit() and len(row[0]) == 14})
        return timestamps

    def _collect_cdx_rows(
        self,
        wildcard_urls: List[str],
        to_timestamp: str,
        limit: int = 30000,
        wait_if_paused: Optional[Callable[[str], None]] = None,
        should_abort: Optional[Callable[[], bool]] = None,
        progress_callback: Optional[Callable[[Dict[str, object]], None]] = None,
        progress_stage: str = "inventory",
    ) -> List[List[str]]:
        dedup: Dict[str, List[str]] = {}
        per_variant_limit = max(300, int(limit / max(len(wildcard_urls), 1)))

        for idx, wildcard in enumerate(wildcard_urls, start=1):
            if should_abort is not None and should_abort():
                raise RuntimeError("Stopped by user")
            if wait_if_paused is not None:
                wait_if_paused(wildcard)
            self._emit_progress(
                progress_callback,
                stage=progress_stage,
                message="Scanning archive index",
                percent=min(24, 8 + int((idx / max(len(wildcard_urls), 1)) * 14)),
                variant=wildcard,
                variants_done=idx - 1,
                variants_total=len(wildcard_urls),
            )
            params = {
                "url": wildcard,
                "output": "json",
                "fl": "timestamp,original,mimetype,length,urlkey",
                "filter": "statuscode:200",
                "collapse": "urlkey",
                "to": to_timestamp,
                "limit": str(per_variant_limit),
            }
            try:
                response = self._get_with_backoff(
                    CDX_API,
                    params=params,
                    timeout=(10, self.timeout),
                    retries=2,
                )
                rows = response.json()
            except requests.RequestException:
                continue

            for row in rows[1:] if len(rows) > 1 else []:
                if len(row) < 5:
                    continue
                key = f"{row[1]}|{row[4]}"
                if key not in dedup:
                    dedup[key] = row
            self._emit_progress(
                progress_callback,
                stage=progress_stage,
                message="Archive index chunk complete",
                percent=min(24, 8 + int((idx / max(len(wildcard_urls), 1)) * 14)),
                found=len(dedup),
                variant=wildcard,
                variants_done=idx,
                variants_total=len(wildcard_urls),
            )

        return list(dedup.values())

    def _merge_variant_snapshots(
        self,
        normalized_url: str,
        progress_callback: Optional[Callable[[Dict[str, object]], None]] = None,
        cdx_limit: int = 20000,
        wait_if_paused: Optional[Callable[[str], None]] = None,
        should_abort: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, object]:
        variants = self._build_url_variants(normalized_url)
        all_ts: set[str] = set()
        ok_ts: set[str] = set()
        variant_rows: List[Dict[str, object]] = []
        failed_variants = 0

        self._emit_progress(
            progress_callback,
            stage="prepare",
            message="Preparing URL variants",
            percent=2,
            variants_done=0,
            variants_total=len(variants),
            current_variant="",
            total_captures=0,
            total_ok=0,
        )

        for idx, variant in enumerate(variants, start=1):
            if should_abort is not None and should_abort():
                raise RuntimeError("Stopped by user")
            if wait_if_paused is not None:
                wait_if_paused(variant)
            self._emit_progress(
                progress_callback,
                stage="variant",
                message="Checking capture list for variant",
                percent=min(95, int((idx / max(len(variants), 1)) * 100) - 5),
                variants_done=idx - 1,
                variants_total=len(variants),
                current_variant=variant,
                total_captures=len(all_ts),
                total_ok=len(ok_ts),
            )
            variant_error = False
            try:
                all_list = self._list_snapshots_adaptive(
                    variant,
                    success_only=False,
                    max_rows=max(500, cdx_limit),
                )
            except RuntimeError:
                all_list = []
                variant_error = True

            try:
                ok_list = self._list_snapshots_adaptive(
                    variant,
                    success_only=True,
                    max_rows=max(500, cdx_limit),
                )
            except RuntimeError:
                ok_list = []
                variant_error = True

            if variant_error:
                failed_variants += 1
            all_ts.update(all_list)
            ok_ts.update(ok_list)
            variant_rows.append(
                {
                    "url": variant,
                    "captures": len(all_list),
                    "ok_captures": len(ok_list),
                }
            )

            self._emit_progress(
                progress_callback,
                stage="variant",
                message="Variant checked",
                percent=min(96, int((idx / max(len(variants), 1)) * 100)),
                variants_done=idx,
                variants_total=len(variants),
                current_variant=variant,
                total_captures=len(all_ts),
                total_ok=len(ok_ts),
            )

        self._emit_progress(
            progress_callback,
            stage="done",
            message="Archive inspection complete",
            percent=100,
            variants_done=len(variants),
            variants_total=len(variants),
            current_variant="",
            total_captures=len(all_ts),
            total_ok=len(ok_ts),
        )

        return {
            "all": sorted(all_ts),
            "ok": sorted(ok_ts),
            "variants": variant_rows,
            "failed_variants": failed_variants,
            "variant_count": len(variants),
        }

    def _list_snapshots_adaptive(self, target_url: str, success_only: bool, max_rows: int) -> List[str]:
        attempts = [max_rows, max(800, max_rows // 2), 800, 500]
        last_error: Optional[Exception] = None
        for rows in attempts:
            try:
                return self._list_snapshots(target_url, success_only=success_only, max_rows=rows, strict=True)
            except RuntimeError as exc:
                last_error = exc
                time.sleep(0.2)
                continue
        if last_error:
            raise RuntimeError(str(last_error))
        raise RuntimeError("Snapshot listing failed")

    def _root_url(self, url: str) -> str:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))

    def _fallback_latest_timestamp(self, target_url: str) -> Optional[str]:
        try:
            res = self._get_with_backoff(
                WAYBACK_AVAILABLE,
                params={"url": target_url},
                timeout=(6, 20),
                retries=1,
            )
            data = res.json()
        except requests.RequestException:
            return None

        nearest = data.get("archived_snapshots", {}).get("closest", {}) if isinstance(data, dict) else {}
        ts = nearest.get("timestamp") if isinstance(nearest, dict) else None
        if isinstance(ts, str) and ts.isdigit() and len(ts) == 14:
            return ts
        return None

    def _fallback_variant_timestamps(self, normalized_url: str) -> List[str]:
        root = self._root_url(normalized_url)
        variants = self._build_url_variants(normalized_url)
        root_variants = self._build_url_variants(root)
        out: List[str] = []
        for candidate in list(dict.fromkeys(variants + root_variants)):
            ts = self._fallback_latest_timestamp(candidate)
            if ts:
                out.append(ts)
        return sorted(set(out))

    def _build_url_variants(self, normalized_url: str) -> List[str]:
        parsed = urlparse(normalized_url)
        host = parsed.netloc.lower()
        path = parsed.path or "/"
        query = parsed.query

        host_variants = [host]
        if host.startswith("www."):
            trimmed = host[4:]
            if trimmed:
                host_variants.append(trimmed)
        elif host.count(".") == 1:
            host_variants.append("www." + host)

        out: List[str] = []
        for scheme in ("https", "http"):
            for h in host_variants:
                candidate = urlunparse((scheme, h, path, "", query, ""))
                cleaned = self._clean_url(candidate)
                if cleaned not in out:
                    out.append(cleaned)
        return out

    def _download_with_repair(self, url: str, latest_timestamp: str) -> Optional[Tuple[bytes, str, str]]:
        preferred = [latest_timestamp] + [
            ts for ts in self._timestamps_for_url(url) if ts != latest_timestamp and ts <= latest_timestamp
        ]
        for ts in preferred:
            result = self._download_at_timestamp(url, ts)
            if result:
                return result
        return None

    def _timestamps_for_url(self, url: str) -> List[str]:
        cached = self._cdx_cache.get(url)
        if cached is not None:
            return cached

        params = {
            "url": url,
            "output": "json",
            "fl": "timestamp",
            "filter": "statuscode:200",
        }
        try:
            response = self._get_with_backoff(
                CDX_API,
                params=params,
                timeout=(10, self.timeout),
                retries=2,
            )
            rows = response.json()
        except requests.RequestException:
            self._cdx_cache[url] = []
            return []

        if len(rows) <= 1:
            self._cdx_cache[url] = []
            return []

        timestamps = sorted(
            {row[0] for row in rows[1:] if row and row[0].isdigit() and len(row[0]) == 14},
            reverse=True,
        )
        self._cdx_cache[url] = timestamps
        return timestamps

    def _download_at_timestamp(self, url: str, timestamp: str) -> Optional[Tuple[bytes, str, str]]:
        archive_url = WAYBACK_RAW.format(timestamp=timestamp, url=url)
        try:
            response = self._get_with_backoff(
                archive_url,
                params=None,
                timeout=(10, self.timeout),
                retries=1,
            )
        except requests.RequestException:
            return None

        body = response.content
        if not body:
            return None
        mime = response.headers.get("content-type", "application/octet-stream").split(";")[0].strip().lower()
        return body, mime, timestamp

    def _discover_links(self, base_url: str, body: bytes, mime: str) -> List[str]:
        links: List[str] = []

        if "text/html" in mime or "application/xhtml" in mime:
            text = self._decode_text(body)
            soup = BeautifulSoup(text, "html.parser")

            for tag in soup.find_all(True):
                for attr in ATTRS_TO_SCAN:
                    value = tag.get(attr)
                    if not value:
                        continue
                    resolved = self._resolve_url(base_url, value)
                    if resolved:
                        links.append(resolved)

                srcset = tag.get("srcset")
                if srcset:
                    for item in srcset.split(","):
                        candidate = item.strip().split(" ")[0]
                        resolved = self._resolve_url(base_url, candidate)
                        if resolved:
                            links.append(resolved)

        elif "text/css" in mime:
            text = self._decode_text(body)
            for match in CSS_URL_RE.findall(text):
                candidate = match.strip().strip("\"'")
                resolved = self._resolve_url(base_url, candidate)
                if resolved:
                    links.append(resolved)

        return list(dict.fromkeys(links))

    def _rewrite_for_offline(
        self,
        output_dir: Path,
        file_path: Path,
        page_url: str,
        mime: str,
        url_to_local: Dict[str, str],
    ) -> None:
        if not file_path.exists():
            return

        if "text/html" in mime or "application/xhtml" in mime:
            text = self._decode_text(file_path.read_bytes())
            soup = BeautifulSoup(text, "html.parser")
            changed = False

            for tag in soup.find_all(True):
                for attr in ATTRS_TO_SCAN:
                    value = tag.get(attr)
                    if not value:
                        continue
                    resolved = self._resolve_url(page_url, value)
                    if not resolved:
                        continue
                    local = url_to_local.get(resolved)
                    if not local:
                        continue
                    tag[attr] = self._relative_link(output_dir, file_path, local)
                    changed = True

                srcset = tag.get("srcset")
                if srcset:
                    parts = []
                    any_change = False
                    for item in srcset.split(","):
                        chunk = item.strip()
                        if not chunk:
                            continue
                        left = chunk.split(" ")
                        candidate = left[0]
                        desc = " ".join(left[1:]) if len(left) > 1 else ""
                        resolved = self._resolve_url(page_url, candidate)
                        local = url_to_local.get(resolved) if resolved else None
                        if local:
                            any_change = True
                            repl = self._relative_link(output_dir, file_path, local)
                            parts.append(f"{repl} {desc}".strip())
                        else:
                            parts.append(chunk)
                    if any_change:
                        tag["srcset"] = ", ".join(parts)
                        changed = True

            if changed:
                file_path.write_text(str(soup), encoding="utf-8")

        elif "text/css" in mime:
            text = self._decode_text(file_path.read_bytes())

            def _replace(match: re.Match[str]) -> str:
                raw = match.group(1)
                value = raw.strip().strip("\"'")
                resolved = self._resolve_url(page_url, value)
                if not resolved:
                    return match.group(0)
                local = url_to_local.get(resolved)
                if not local:
                    return match.group(0)
                rel = self._relative_link(output_dir, file_path, local)
                return f"url('{rel}')"

            rewritten = CSS_URL_RE.sub(_replace, text)
            if rewritten != text:
                file_path.write_text(rewritten, encoding="utf-8")

    def _relative_link(self, output_dir: Path, file_path: Path, target_local: str) -> str:
        target_abs = output_dir / target_local
        return os.path.relpath(target_abs, file_path.parent).replace("\\", "/")

    def _save_file(self, output_dir: Path, url: str, body: bytes, mime: str) -> Tuple[Path, str]:
        local_rel = self._local_path_for_url(url, mime)
        local_abs = output_dir / local_rel
        local_abs.parent.mkdir(parents=True, exist_ok=True)
        local_abs.write_bytes(body)
        return local_abs, local_rel

    def _local_path_for_url(self, url: str, mime: str) -> str:
        parsed = urlparse(url)
        path = parsed.path or "/"
        parts = [self._safe_name(p) for p in path.split("/") if p]

        if not parts:
            parts = ["index.html"]
        elif path.endswith("/"):
            parts.append("index.html")

        filename = parts[-1]
        if "." not in filename and ("text/html" in mime or "application/xhtml" in mime):
            parts[-1] = f"{filename}.html"

        if parsed.query:
            stem, ext = os.path.splitext(parts[-1])
            query_hash = hashlib.sha1(parsed.query.encode("utf-8")).hexdigest()[:8]
            parts[-1] = f"{stem}__q_{query_hash}{ext}"

        if len(parts) > 1:
            return "/".join(parts)
        return parts[0]

    def _safe_name(self, text: str) -> str:
        value = SAFE_NAME_RE.sub("_", text.strip())
        value = value.strip("._")
        return value or "file"

    def _resolve_url(self, base_url: str, value: str) -> Optional[str]:
        candidate = value.strip()
        if not candidate:
            return None
        lowered = candidate.lower()
        if lowered.startswith(BAD_SCHEMES):
            return None

        resolved = urljoin(base_url, candidate)
        parsed = urlparse(resolved)
        if parsed.scheme not in ("http", "https"):
            return None
        return self._clean_url(resolved)

    def _clean_url(self, url: str) -> str:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))

    def _normalize_target(self, target_url: str) -> str:
        url = target_url.strip()
        if not url:
            raise RuntimeError("URL is required")
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
        parsed = urlparse(url)
        if not parsed.netloc:
            raise RuntimeError("Invalid URL")
        return self._clean_url(url)

    def _is_same_host(self, root_url: str, other_url: str) -> bool:
        return urlparse(root_url).netloc == urlparse(other_url).netloc

    def _is_allowed_host(self, allowed_hosts: set[str], other_url: str) -> bool:
        return urlparse(other_url).netloc in allowed_hosts

    def _wildcard_url(self, root_url: str) -> str:
        parsed = urlparse(root_url)
        return f"{parsed.scheme}://{parsed.netloc}/*"

    def _extension_of_url(self, value: str) -> str:
        path = urlparse(value).path
        base = path.rsplit("/", 1)[-1]
        if "." not in base:
            return "(none)"
        ext = base.rsplit(".", 1)[-1].lower()
        if not ext or len(ext) > 8:
            return "(none)"
        return "." + ext

    def _folder_of_path(self, path: str) -> str:
        clean = path if path else "/"
        if clean == "/":
            return "/"
        if clean.endswith("/"):
            return clean
        if "/" not in clean[1:]:
            return "/"
        return clean.rsplit("/", 1)[0] + "/"

    def _looks_like_page(self, path: str, mime: str) -> bool:
        lower = path.lower()
        if lower.endswith((".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".woff", ".woff2", ".ttf", ".ico", ".mp4", ".webm", ".pdf", ".zip")):
            return False
        if "text/html" in mime or "application/xhtml" in mime:
            return True
        if lower.endswith((".html", ".htm", ".php", ".asp", ".aspx")):
            return True
        return lower.endswith("/")

    def _prioritize_inventory_urls(self, rows: List[List[str]], allowed_hosts: set[str]) -> List[str]:
        scored: List[Tuple[int, str]] = []
        for row in rows:
            if len(row) < 5:
                continue
            url = row[1]
            if urlparse(url).netloc not in allowed_hosts:
                continue
            mime = (row[2] or "").lower()
            path = urlparse(url).path or "/"

            score = 0
            if self._looks_like_page(path, mime):
                score += 30
            if "/wp-content/uploads/" in path.lower():
                score -= 10
            if self._extension_of_url(url) in (".css", ".js"):
                score += 15
            if path == "/" or path.endswith("/index.html"):
                score += 20
            if len(path) < 35:
                score += 5
            scored.append((score, self._clean_url(url)))

        scored.sort(key=lambda item: item[0], reverse=True)
        ordered = [url for _, url in scored]
        return list(dict.fromkeys(ordered))

    def _extract_wp_slug(self, path: str, marker: str) -> Optional[str]:
        if marker not in path:
            return None
        tail = path.split(marker, 1)[1]
        slug = tail.split("/", 1)[0].strip()
        if not slug:
            return None
        return slug

    def _extract_wp_json_route(self, path: str) -> Optional[str]:
        marker = "/wp-json/"
        if marker not in path:
            return None
        tail = path.split(marker, 1)[1].strip("/")
        if not tail:
            return ""
        return tail

    def _guess_site_type(self, signals: List[str]) -> str:
        if not signals:
            return "Static/Unknown"

        rank = {
            "wordpress": "WordPress",
            "wix": "Wix",
            "shopify": "Shopify",
            "php": "PHP site",
            "spa": "SPA/React-like",
        }
        counts: Dict[str, int] = {}
        for item in signals:
            counts[item] = counts.get(item, 0) + 1

        best = sorted(counts.items(), key=lambda x: x[1], reverse=True)[0][0]
        return rank.get(best, "Static/Unknown")

    def _human_size(self, size: int) -> str:
        if size <= 0:
            return "0 B"
        units = ["B", "KB", "MB", "GB", "TB"]
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{int(value)} {unit}"
                return f"{value:.2f} {unit}"
            value /= 1024
        return f"{size} B"

    def _decode_text(self, body: bytes) -> str:
        for encoding in ("utf-8", "latin-1"):
            try:
                return body.decode(encoding)
            except UnicodeDecodeError:
                continue
        return body.decode("utf-8", errors="ignore")

    def _build_calendar(self, snapshots: List[str]) -> List[Dict[str, object]]:
        month_names = {
            "01": "Jan",
            "02": "Feb",
            "03": "Mar",
            "04": "Apr",
            "05": "May",
            "06": "Jun",
            "07": "Jul",
            "08": "Aug",
            "09": "Sep",
            "10": "Oct",
            "11": "Nov",
            "12": "Dec",
        }
        years: Dict[str, Dict[str, Dict[str, Dict[str, object]]]] = {}

        for ts in snapshots:
            year = ts[0:4]
            month = ts[4:6]
            day = ts[6:8]
            time_code = ts[8:14]
            if year not in years:
                years[year] = {}
            if month not in years[year]:
                years[year][month] = {}
            if day not in years[year][month]:
                years[year][month][day] = {
                    "day": day,
                    "count": 0,
                    "timestamp": ts,
                    "times": [],
                }

            years[year][month][day]["count"] = int(years[year][month][day]["count"]) + 1
            years[year][month][day]["timestamp"] = ts
            years[year][month][day]["times"].append(
                {
                    "timestamp": ts,
                    "label": f"{time_code[0:2]}:{time_code[2:4]}:{time_code[4:6]}",
                }
            )

        out: List[Dict[str, object]] = []
        for year in sorted(years.keys(), reverse=True):
            months_out: List[Dict[str, object]] = []
            for month in sorted(years[year].keys(), reverse=True):
                day_items = [
                    years[year][month][k]
                    for k in sorted(years[year][month].keys(), key=lambda d: int(d))
                ]
                months_out.append(
                    {
                        "month": month,
                        "month_label": month_names.get(month, month),
                        "days": day_items,
                    }
                )
            out.append({"year": year, "months": months_out})

        return out
