from __future__ import annotations

import unittest

from app import app


class AppSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_home_page_loads(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Archive Web Offline Tool", response.data)

    def test_start_routes_require_url(self) -> None:
        start_routes = [
            "/inspect/start",
            "/analyze/start",
            "/analyze-batch/start",
            "/check/start",
            "/sitemap/start",
            "/download/start",
            "/download-missing/start",
        ]
        for route in start_routes:
            with self.subTest(route=route):
                response = self.client.post(route, data={})
                self.assertEqual(response.status_code, 400)

    def test_status_routes_reject_unknown_job(self) -> None:
        status_routes = [
            "/inspect/status/does-not-exist",
            "/analyze/status/does-not-exist",
            "/analyze-batch/status/does-not-exist",
            "/check/status/does-not-exist",
            "/sitemap/status/does-not-exist",
            "/download/status/does-not-exist",
            "/download-missing/status/does-not-exist",
        ]
        for route in status_routes:
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 404)

    def test_delete_recent_project_requires_target_url(self) -> None:
        response = self.client.post("/recent-projects/delete", json={})
        self.assertEqual(response.status_code, 400)

    def test_project_data_status_requires_target_url(self) -> None:
        response = self.client.get("/project/data-status")
        self.assertEqual(response.status_code, 400)

    def test_project_data_status_unknown_url_returns_ok_payload(self) -> None:
        response = self.client.get("/project/data-status", query_string={"target_url": "https://example.com/none"})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get("ok"))
        self.assertIn("status", payload)

    def test_diagnostics_endpoint_returns_ok_payload(self) -> None:
        response = self.client.get("/diagnostics")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get("ok"))
        self.assertIn("runtime", payload)

    def test_project_open_without_target_url_shows_error_page(self) -> None:
        response = self.client.get("/project/open")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"target_url is required", response.data)

    def test_delete_recent_project_with_delete_files_flag(self) -> None:
        response = self.client.post(
            "/recent-projects/delete",
            json={"target_url": "https://example.com/nope", "delete_output_files": True},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get("ok"))
        self.assertIn("output_deleted", payload)

    def test_delete_preview_requires_target_url(self) -> None:
        response = self.client.get("/recent-projects/delete-preview")
        self.assertEqual(response.status_code, 400)

    def test_delete_preview_returns_ok_payload(self) -> None:
        response = self.client.get("/recent-projects/delete-preview", query_string={"target_url": "https://example.com/nope"})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get("ok"))
        self.assertIn("deletable", payload)

    def test_settings_page_loads(self) -> None:
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Settings", response.data)

    def test_settings_save_works(self) -> None:
        response = self.client.post(
            "/settings",
            data={
                "theme_accent": "#8b5e3c",
                "theme_accent_2": "#6d4a30",
                "theme_success": "#4a7c59",
                "theme_bg": "#f8f5f0",
                "theme_card": "#ffffff",
                "theme_text": "#2d241c",
                "theme_muted": "#6b5b4d",
                "theme_border": "#e0d6c8",
                "default_output_root": "./output",
                "default_display_limit": "10",
                "default_inspect_cdx_limit": "1500",
                "default_analyze_cdx_limit": "12000",
                "default_max_files": "400",
                "default_missing_limit": "300",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Settings saved", response.data)

    def test_check_preflight_requires_url(self) -> None:
        response = self.client.get("/check/preflight")
        self.assertEqual(response.status_code, 400)

    def test_check_preflight_returns_payload(self) -> None:
        response = self.client.get(
            "/check/preflight",
            query_string={
                "target_url": "https://example.com",
                "selected_snapshot": "20200101000000",
                "output_root": "./output",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertTrue(payload.get("ok"))
        self.assertIn("manifest_found", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
