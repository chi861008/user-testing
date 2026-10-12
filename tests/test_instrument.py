import base64
import importlib
import io
import os
import tempfile
import unittest


class InstrumentFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.temp.name
        os.environ["ADMIN_PASSWORD"] = "test-admin-password"
        cls.server = importlib.import_module("server")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.client = self.server.app.test_client()
        self.assertEqual(self.client.post("/api/login", json={"password": "test-admin-password"}).status_code, 200)

    def test_google_site_instrumentation_and_screenshot(self):
        response = self.client.post("/api/studies/url", json={"name": "Google site", "url": "https://example.com/app", "mode": "web"})
        self.assertEqual(response.status_code, 200)
        sid = response.json["id"]
        task = {"id": "task-1", "prompt": "完成訂單", "cond": {"type": "event", "text": "ordered"},
                "paths": [["open-menu", "submit-order"], ["quick-order"]]}
        self.assertEqual(self.client.put(f"/api/studies/{sid}/tasks", json={"tasks": [task]}).status_code, 200)
        run_response = self.client.post(f"/api/public/study/{sid}/run", json={"role": "tester"})
        self.assertEqual(run_response.status_code, 200)
        run = run_response.json["run"]
        self.assertIn("ut_run=", run_response.json["url"])
        endpoint = f"/api/instrument/{run}"
        self.assertEqual(self.client.get(endpoint, headers={"Origin": "https://untrusted.example"}).status_code, 403)
        headers = {"Origin": "https://example.com"}
        self.assertEqual(self.client.post(endpoint, json={"events": [{"type": "ready"}]}, headers=headers).status_code, 200)
        self.assertTrue(self.client.get(f"/api/public/run/{run}/status").json["ready"])
        self.assertEqual(self.client.post(f"/api/public/run/{run}/task", json={"taskId": "task-1"}).status_code, 200)
        self.assertEqual(self.client.get(endpoint, headers=headers).json["cond"]["text"], "ordered")
        events = [
            {"type": "click", "data": {"element": "open-menu", "screen": "/app", "x": .2, "y": .4, "w": 390, "h": 844, "interactive": True, "password": "must-not-save"}},
            {"type": "click", "data": {"element": "submit-order", "screen": "/app", "x": .5, "y": .7, "w": 390, "h": 844, "interactive": True}},
            {"type": "active", "data": {"ms": 1500}}, {"type": "complete"},
        ]
        self.assertEqual(self.client.post(endpoint, json={"events": events}, headers=headers).status_code, 200)
        self.assertTrue(self.client.get(f"/api/public/run/{run}/status").json["success"])
        result = self.client.post(f"/api/public/run/{run}/finish", json={}).json
        self.assertTrue(result["success"])
        self.assertEqual([x["element"] for x in result["clicks"]], ["open-menu", "submit-order"])
        self.assertNotIn("must-not-save", str(result))
        self.assertEqual(result["clicks"][0]["device"], "mobile")
        self.assertGreaterEqual(result["activeMs"], 0)
        submitted = self.client.post(f"/api/public/study/{sid}/session", json={"runId": run, "role": "tester", "results": [result]})
        self.assertEqual(submitted.status_code, 200)
        self.assertTrue(self.client.get(endpoint, headers=headers).json["ended"])

        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lX8AAAAASUVORK5CYII=")
        upload = self.client.post(f"/api/studies/{sid}/screenshots", data={"taskId": "task-1", "screen": "/app", "device": "mobile", "file": (io.BytesIO(png), "screen.png")})
        self.assertEqual(upload.status_code, 200)
        image = self.client.get(f"/api/studies/{sid}/screenshots/task-1/mobile?screen=%2Fapp")
        self.assertEqual(image.data, png)

    def test_liff_recording(self):
        response = self.client.post("/api/studies/url", json={"name": "LIFF site", "url": "https://liff.line.me/1234567890-AbcdEfgh", "mode": "liff", "endpoint": "https://liff-app.example.com/"})
        self.assertEqual(response.status_code, 200)
        sid = response.json["id"]
        self.client.put(f"/api/studies/{sid}/tasks", json={"tasks": [{"id": "task-2", "prompt": "找資料", "cond": {"type": "marker", "text": "found"}}]})
        recording = self.client.post(f"/api/studies/{sid}/recordings", json={"taskId": "task-2"}).json
        self.assertIn("ut_run=", recording["url"])
        endpoint = f"/api/instrument/{recording['run']}"
        self.assertEqual(self.client.post(endpoint, json={"events": [{"type": "ready"}, {"type": "click", "data": {"element": "search", "screen": "/", "w": 390}}]}, headers={"Origin": "https://liff-app.example.com"}).status_code, 200)
        finished = self.client.post(f"/api/studies/{sid}/recordings/{recording['run']}/finish").json
        self.assertEqual(finished["path"], ["search"])
        self.assertTrue(finished["ready"])


if __name__ == "__main__":
    unittest.main()
