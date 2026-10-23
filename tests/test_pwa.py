import json
import unittest
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from app.main import app


class PwaAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def test_manifest_and_install_icons_are_valid(self) -> None:
        response = self.client.get("/manifest.webmanifest")

        self.assertEqual(response.status_code, 200)
        self.assertIn("application/manifest+json", response.headers["content-type"])
        self.assertIn("no-store", response.headers["cache-control"])
        manifest = json.loads(response.text)
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/?source=pwa")
        self.assertEqual(manifest["scope"], "/")

        expected_sizes = {
            "/icons/friday-192.png": (192, 192),
            "/icons/friday-512.png": (512, 512),
            "/icons/friday-maskable-512.png": (512, 512),
        }
        for icon in manifest["icons"]:
            icon_response = self.client.get(icon["src"])
            self.assertEqual(icon_response.status_code, 200)
            with Image.open(BytesIO(icon_response.content)) as image:
                self.assertEqual(image.size, expected_sizes[icon["src"]])

    def test_dashboard_declares_pwa_metadata(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn('rel="manifest" href="/manifest.webmanifest"', response.text)
        self.assertIn('rel="apple-touch-icon" href="/icons/friday-180.png"', response.text)
        self.assertIn('name="apple-mobile-web-app-capable" content="yes"', response.text)
        self.assertIn('/app.js?v=arc-17', response.text)
        self.assertIn('/styles.css?v=arc-17', response.text)
        self.assertIn('id="fileInput"', response.text)
        self.assertIn('id="attachButton"', response.text)
        self.assertIn('id="research"', response.text)
        self.assertIn('id="mentionMenu"', response.text)

    def test_service_worker_caches_shell_but_bypasses_api(self) -> None:
        response = self.client.get("/service-worker.js")

        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response.headers["content-type"])
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertIn('const SHELL_CACHE = "friday-shell-v6"', response.text)
        self.assertIn('url.pathname.startsWith("/api/")', response.text)
        self.assertIn('request.method !== "GET"', response.text)
        self.assertNotIn('cache.put("/api/', response.text)


if __name__ == "__main__":
    unittest.main()
