from __future__ import annotations

import time
import unittest
from dataclasses import asdict
from unittest.mock import patch

import app as web_app
from archiver import ArchiveResult


class AsyncRoutesSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        web_app.app.config["TESTING"] = True
        self.client = web_app.app.test_client()
        self.target_url = "https://example.com/smoke-async"
        self.snapshot = "20240101120000"

    def _poll_status(self, path: str, timeout_s: float = 5.0) -> dict:
        started = time.time()
        last = {}
        while time.time() - started < timeout_s:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            last = response.get_json() or {}
            if last.get("state") in ("done", "error"):
                return last
            time.sleep(0.05)
        self.fail(f"Timeout waiting for {path}. Last payload: {last}")

    def _assert_progress_shape(self, payload: dict) -> None:
        progress = payload.get("progress") or {}
        self.assertIn("stage", progress)
        self.assertIn("message", progress)
        self.assertIn("percent", progress)
        self.assertIn("current_item", progress)
        self.assertIn("elapsed_seconds", progress)

    def _fake_inspect(self, *_args, **_kwargs) -> dict:
        return {
            "target_url": self.target_url,
            "inspected_scope": self.target_url,
            "total_snapshots": 2,
            "total_ok_snapshots": 2,
            "latest_snapshot": self.snapshot,
            "latest_ok_snapshot": self.snapshot,
            "first_snapshot": "20230101120000",
            "snapshots": [self.snapshot, "20230101120000"],
            "calendar": {},
            "variants": [{"url": self.target_url, "captures": 2, "ok_captures": 2}],
            "display_limit": 10,
            "cdx_limit": 1500,
            "limited_mode": True,
            "fallback_used": False,
        }

    def _fake_analyze(self, *_args, **_kwargs) -> dict:
        return {
            "target_url": self.target_url,
            "selected_snapshot": self.snapshot,
            "estimated_files": 12,
            "estimated_size_bytes": 34567,
            "estimated_size_human": "33.8 KB",
            "site_type": "WordPress",
            "top_mime_types": [["text/html", 5]],
            "top_extensions": [[".html", 5]],
            "top_folders": [["/", 5], ["/wp-content/", 4]],
            "site_pages": ["/", "/about/", "/blog/post-1/"],
            "variants_checked": [{"url": self.target_url, "captures": 2, "ok_captures": 2}],
            "wordpress": {
                "detected": True,
                "themes": ["twentytwentyfour"],
                "plugins": ["seo-plugin"],
                "categories": ["news"],
                "tags": ["release"],
                "post_types": ["posts"],
                "blog_posts": ["/2024/01/post-1/"],
                "wp_json_routes": ["wp/v2/posts"],
            },
        }

    def _fake_audit(self, *_args, **_kwargs) -> dict:
        return {
            "target_url": self.target_url,
            "snapshot": self.snapshot,
            "output_dir": "output/fake",
            "expected_count": 12,
            "downloaded_count": 10,
            "have_count": 9,
            "missing_count": 3,
            "extra_count": 1,
            "coverage_percent": 75.0,
            "downloaded_size_bytes": 12345,
            "downloaded_size_human": "12.1 KB",
            "have_urls": ["https://example.com/"],
            "missing_urls": ["https://example.com/missing.css"],
            "extra_urls": [],
        }

    def _fake_download_missing(self, *_args, **_kwargs) -> dict:
        return {
            "snapshot": self.snapshot,
            "output_dir": "output/fake",
            "attempted": 3,
            "added": 2,
            "failed": 1,
            "recovered": 1,
            "bytes_added": 2000,
            "bytes_added_human": "2.0 KB",
            "seconds": 0.1,
        }

    def _fake_run(self, *_args, **_kwargs) -> ArchiveResult:
        return ArchiveResult(
            target_url=self.target_url,
            latest_snapshot=self.snapshot,
            total_snapshots=2,
            output_dir="output/fake",
            files_downloaded=10,
            files_recovered=1,
            missing_urls=["https://example.com/missing.css"],
            files=[],
            expected_sample_files=12,
            expected_sample_size_bytes=34567,
            coverage_percent=75.0,
            missing_expected_urls=["https://example.com/missing.css"],
            seconds=0.2,
        )

    def test_async_routes_start_and_reach_terminal_state(self) -> None:
        with patch.object(web_app.tool, "inspect", side_effect=self._fake_inspect), patch.object(
            web_app.tool, "analyze", side_effect=self._fake_analyze
        ), patch.object(web_app.tool, "audit", side_effect=self._fake_audit), patch.object(
            web_app.tool, "download_missing", side_effect=self._fake_download_missing
        ), patch.object(web_app.tool, "run", side_effect=self._fake_run):
            inspect_start = self.client.post(
                "/inspect/start",
                data={"target_url": self.target_url, "display_limit": "10", "cdx_limit": "1500", "force_refresh": "1"},
            ).get_json()
            self.assertTrue(inspect_start["ok"])
            inspect_done = self._poll_status(f"/inspect/status/{inspect_start['job_id']}")
            self.assertEqual(inspect_done.get("state"), "done")
            self._assert_progress_shape(inspect_done)

            analyze_start = self.client.post(
                "/analyze/start",
                data={
                    "target_url": self.target_url,
                    "selected_snapshot": self.snapshot,
                    "display_limit": "10",
                    "cdx_limit": "1500",
                },
            ).get_json()
            self.assertTrue(analyze_start["ok"])
            analyze_done = self._poll_status(f"/analyze/status/{analyze_start['job_id']}")
            self.assertEqual(analyze_done.get("state"), "done")
            self._assert_progress_shape(analyze_done)

            batch_start = self.client.post(
                "/analyze-batch/start",
                data={
                    "target_url": self.target_url,
                    "display_limit": "10",
                    "inspect_cdx_limit": "1500",
                    "analyze_cdx_limit": "1500",
                    "analyze_count": "2",
                },
            ).get_json()
            self.assertTrue(batch_start["ok"])
            batch_done = self._poll_status(f"/analyze-batch/status/{batch_start['job_id']}")
            self.assertEqual(batch_done.get("state"), "done")
            self._assert_progress_shape(batch_done)

            sitemap_start = self.client.post(
                "/sitemap/start",
                data={"target_url": self.target_url, "selected_snapshot": self.snapshot, "cdx_limit": "1500"},
            ).get_json()
            self.assertTrue(sitemap_start["ok"])
            sitemap_done = self._poll_status(f"/sitemap/status/{sitemap_start['job_id']}")
            self.assertEqual(sitemap_done.get("state"), "done")
            self._assert_progress_shape(sitemap_done)

            check_start = self.client.post(
                "/check/start",
                data={"target_url": self.target_url, "selected_snapshot": self.snapshot},
            ).get_json()
            self.assertTrue(check_start["ok"])
            check_done = self._poll_status(f"/check/status/{check_start['job_id']}")
            self.assertEqual(check_done.get("state"), "done")
            self._assert_progress_shape(check_done)

            download_start = self.client.post(
                "/download/start",
                data={"target_url": self.target_url, "selected_snapshot": self.snapshot, "max_files": "100"},
            ).get_json()
            self.assertTrue(download_start["ok"])
            download_done = self._poll_status(f"/download/status/{download_start['job_id']}")
            self.assertEqual(download_done.get("state"), "done")
            self._assert_progress_shape(download_done)
            self.assertIn("result", download_done)
            self.assertEqual(download_done["result"].get("files_downloaded"), asdict(self._fake_run()).get("files_downloaded"))

            missing_start = self.client.post(
                "/download-missing/start",
                data={"target_url": self.target_url, "selected_snapshot": self.snapshot, "missing_limit": "20"},
            ).get_json()
            self.assertTrue(missing_start["ok"])
            missing_done = self._poll_status(f"/download-missing/status/{missing_start['job_id']}")
            self.assertEqual(missing_done.get("state"), "done")
            self._assert_progress_shape(missing_done)


if __name__ == "__main__":
    unittest.main(verbosity=2)
