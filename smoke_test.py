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


if __name__ == "__main__":
    unittest.main(verbosity=2)
