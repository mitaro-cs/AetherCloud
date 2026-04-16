import io
import os
import tempfile
import unittest
from pathlib import Path

from aethercloud import create_app


class AetherCloudAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        base = Path(self.temp_dir.name)
        os.environ["AETHER_DB_PATH"] = str(base / "users.db")
        os.environ["AETHER_STORAGE_DIR"] = str(base / "storage")
        os.environ["AETHER_SECRET_KEY"] = "test-secret"
        os.environ["AETHER_TOTAL_STORAGE_GB"] = "4"
        os.environ["AETHER_MAX_UPLOAD_MB"] = "8"
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        for key in [
            "AETHER_DB_PATH",
            "AETHER_STORAGE_DIR",
            "AETHER_SECRET_KEY",
            "AETHER_TOTAL_STORAGE_GB",
            "AETHER_MAX_UPLOAD_MB",
        ]:
            os.environ.pop(key, None)
        self.temp_dir.cleanup()

    def register(self):
        return self.client.post(
            "/register",
            data={
                "fullname": "Test User",
                "email": "test@example.com",
                "password": "Password!1",
                "confirm_password": "Password!1",
            },
            follow_redirects=False,
        )

    def login(self):
        return self.client.post(
            "/login",
            data={"email": "test@example.com", "password": "Password!1"},
            follow_redirects=False,
        )

    def test_public_routes_render(self):
        for route in ["/", "/register", "/login", "/forgot", "/health", "/manifest.webmanifest", "/favicon.svg"]:
            response = self.client.get(route)
            self.assertEqual(response.status_code, 200, route)
            response.close()

    def test_register_login_and_workspace_views(self):
        register_response = self.register()
        self.assertEqual(register_response.status_code, 302)
        self.assertIn("/login", register_response.headers["Location"])

        with self.client:
            login_response = self.login()
            self.assertEqual(login_response.status_code, 302)
            self.assertIn("/cloud", login_response.headers["Location"])

            home = self.client.get("/cloud?view=home")
            storage = self.client.get("/cloud?view=storage")
            settings = self.client.get("/cloud?view=settings")

            self.assertEqual(home.status_code, 200)
            self.assertIn("Command Center", home.get_data(as_text=True))
            self.assertEqual(storage.status_code, 200)
            self.assertIn("Workspace Tree", storage.get_data(as_text=True))
            self.assertEqual(settings.status_code, 200)
            self.assertIn("Change password", settings.get_data(as_text=True))
            home.close()
            storage.close()
            settings.close()

    def test_folder_creation_and_upload_flow(self):
        self.register()

        with self.client:
            self.login()

            root_page = self.client.get("/cloud")
            self.assertEqual(root_page.status_code, 200)

            create_folder = self.client.post(
                "/cloud/folders",
                data={
                    "parent_id": 1,
                    "next_view": "storage",
                    "next_tab": "folders",
                    "name": "Design Vault",
                },
                follow_redirects=True,
            )
            page = create_folder.get_data(as_text=True)
            self.assertEqual(create_folder.status_code, 200)
            self.assertIn("Design Vault", page)
            create_folder.close()

            upload = self.client.post(
                "/cloud/files/upload",
                data={
                    "folder_id": 1,
                    "next_view": "storage",
                    "next_tab": "folders",
                    "files": (io.BytesIO(b"hello cloud"), "hello.txt"),
                },
                content_type="multipart/form-data",
                follow_redirects=True,
            )
            upload_page = upload.get_data(as_text=True)
            self.assertEqual(upload.status_code, 200)
            self.assertIn("Uploaded: 1 file(s).", upload_page)
            self.assertIn("hello.txt", upload_page)
            upload.close()


if __name__ == "__main__":
    unittest.main()
