"""Integration checks for device registration and separate admin authentication."""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

API_TOKEN = "test-device-write-token-" + "a" * 40
ADMIN_TOKEN = "test-admin-only-token-" + "b" * 40
TEMP_DIR = tempfile.TemporaryDirectory()
DB_FILE = str(Path(TEMP_DIR.name) / "status.db")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["STATUS_API_TOKEN"] = API_TOKEN
os.environ["STATUS_ADMIN_TOKEN"] = ADMIN_TOKEN
os.environ["STATUS_DB_PATH"] = DB_FILE

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

main.API_TOKEN = API_TOKEN
main.ADMIN_TOKEN = ADMIN_TOKEN
main.DB_PATH = DB_FILE
main.startup()


class DeviceAdminTests(unittest.TestCase):
    def setUp(self):
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM devices")
        self.client = TestClient(main.app, base_url="https://testserver")

    def tearDown(self):
        self.client.close()

    def test_admin_data_requires_https_and_admin_auth(self):
        insecure = TestClient(main.app, base_url="http://testserver")
        try:
            response = insecure.get("/admin/api/devices", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
            self.assertEqual(response.status_code, 400)
            login = insecure.post("/admin/login", json={"token": ADMIN_TOKEN})
            self.assertEqual(login.status_code, 400)
        finally:
            insecure.close()

        unauthorized = self.client.get("/admin/api/devices")
        self.assertEqual(unauthorized.status_code, 401)

    def test_admin_token_and_api_token_are_separate(self):
        login_with_device_token = self.client.post(
            "/admin/login",
            headers={"X-Forwarded-Proto": "https"},
            json={"token": API_TOKEN},
        )
        self.assertEqual(login_with_device_token.status_code, 401)

        api_with_admin_token = self.client.post(
            "/api/v1/devices/heartbeat",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            json={"device_id": "device-test"},
        )
        self.assertEqual(api_with_admin_token.status_code, 401)

    def test_heartbeat_registers_device_and_admin_session_lists_it(self):
        login = self.client.post(
            "/admin/login",
            headers={"X-Forwarded-Proto": "https"},
            json={"token": ADMIN_TOKEN},
        )
        self.assertEqual(login.status_code, 200)
        cookie = login.headers.get("set-cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=strict", cookie)

        heartbeat = self.client.post(
            "/api/v1/devices/heartbeat",
            headers={"Authorization": f"Bearer {API_TOKEN}"},
            json={
                "device_id": "device-test-123",
                "device_name": "Test Android",
                "platform": "android",
            },
        )
        self.assertEqual(heartbeat.status_code, 200)

        listed = self.client.get("/admin/api/devices")
        self.assertEqual(listed.status_code, 200)
        payload = listed.json()
        self.assertEqual(payload["summary"]["total_devices"], 1)
        self.assertEqual(payload["summary"]["online_devices"], 1)
        self.assertEqual(payload["devices"][0]["device_id"], "device-test-123")
        self.assertEqual(payload["devices"][0]["event_count"], 0)

    def test_canonical_and_legacy_event_routes_keep_working(self):
        headers = {
            "Authorization": f"Bearer {API_TOKEN}",
            "Idempotency-Key": "event-test-1",
        }
        body = {
            "event_id": "event-test-1",
            "event_time": int(time.time() * 1000),
            "event_type": "music",
            "state": "playing",
            "title": "Test song",
            "device_id": "device-test-123",
            "device_name": "Test Android",
            "platform": "android",
        }
        canonical = self.client.post("/api/v1/events", headers=headers, json=body)
        self.assertEqual(canonical.status_code, 200)
        self.assertFalse(canonical.json()["duplicate"])

        legacy_body = dict(body, event_id="event-test-2")
        legacy_headers = dict(headers, **{"Idempotency-Key": "event-test-2"})
        legacy = self.client.post("/api/v1/evens", headers=legacy_headers, json=legacy_body)
        self.assertEqual(legacy.status_code, 200)

        duplicate = self.client.post("/api/v1/events", headers=headers, json=body)
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json()["duplicate"])

        with TestClient(main.app, base_url="https://testserver") as authenticated:
            login = authenticated.post(
                "/admin/login",
                headers={"X-Forwarded-Proto": "https"},
                json={"token": ADMIN_TOKEN},
            )
            self.assertEqual(login.status_code, 200)
            listed = authenticated.get("/admin/api/devices").json()
            self.assertEqual(listed["summary"]["total_devices"], 1)
            self.assertEqual(listed["devices"][0]["event_count"], 2)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        TEMP_DIR.cleanup()
