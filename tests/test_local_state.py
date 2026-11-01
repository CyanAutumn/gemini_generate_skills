"""Offline tests for local state; no browser or account access."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / "gemini-browser" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from local_state import ProfileLock, RunStore, default_data_dir, read_json, verify_image, write_json


class LocalStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_default_data_directory(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            self.assertEqual(default_data_dir(), self.root / "GeminiBrowserSkill")

    def test_atomic_json_replacement_and_failure_preserves_old_record(self):
        path = self.root / "nested" / "record.json"
        write_json(path, {"prompt": "海边", "version": 1})
        write_json(path, {"prompt": "山间", "version": 2})
        self.assertEqual(read_json(path), {"prompt": "山间", "version": 2})
        with patch("local_state.os.replace", side_effect=OSError("模拟写入失败")):
            with self.assertRaises(OSError):
                write_json(path, {"version": 3})
        self.assertEqual(read_json(path)["version"], 2)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_run_record_survives_new_store_instance(self):
        store = RunStore(self.root / "outputs")
        record = store.create("画一只猫", self.root / "profile")
        self.assertEqual(record["status"], "prepared")
        self.assertIsNone(record["chat_url"])
        self.assertEqual(record["files"], [])
        record["status"] = "completed"
        record["files"] = [{"path": "cat.png"}]
        store.save(record)
        restored = RunStore(self.root / "outputs").load(record["id"])
        self.assertEqual(restored, record)
        self.assertTrue(store.path_for(record["id"]).is_file())
        with self.assertRaises(ValueError):
            store.path_for("../escape")

    def test_profile_lock_excludes_other_process_and_releases(self):
        profile = self.root / "profile"
        code = (
            "import sys; "
            f"sys.path.insert(0, {str(SCRIPTS)!r}); "
            "from local_state import ProfileLock; "
            "lock = ProfileLock(sys.argv[1]); lock.__enter__(); lock.__exit__(None, None, None)"
        )
        with ProfileLock(profile):
            with self.assertRaises(RuntimeError):
                with ProfileLock(profile):
                    pass
            blocked = subprocess.run(
                [sys.executable, "-X", "utf8", "-c", code, str(profile)],
                capture_output=True, text=True, encoding="utf-8",
                timeout=15,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("浏览器资料目录正在使用", blocked.stderr)
        released = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", code, str(profile)],
            capture_output=True, text=True, encoding="utf-8",
            timeout=15,
        )
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertTrue((profile / ".gemini-browser.lock").exists())

    def test_profile_lock_releases_after_exception(self):
        profile = self.root / "profile"
        with self.assertRaisesRegex(ValueError, "模拟失败"):
            with ProfileLock(profile):
                raise ValueError("模拟失败")
        with ProfileLock(profile):
            pass

    def test_valid_image_formats_and_metadata(self):
        for image_format, suffix in [("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp"), ("GIF", "gif")]:
            with self.subTest(format=image_format):
                path = self.root / f"image.{suffix}"
                Image.new("RGB", (37, 29), "blue").save(path, format=image_format)
                result = verify_image(path)
                self.assertEqual((result["width"], result["height"]), (37, 29))
                self.assertEqual(result["format"], image_format)
                self.assertEqual(result["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertEqual(result["size_bytes"], path.stat().st_size)

    def test_corrupt_and_unsupported_images_are_rejected(self):
        corrupt = self.root / "broken.png"
        corrupt.write_bytes(b"not an image")
        with self.assertRaises(ValueError):
            verify_image(corrupt)
        jpeg = self.root / "truncated.jpg"
        Image.new("RGB", (64, 64), "red").save(jpeg, format="JPEG")
        jpeg.write_bytes(jpeg.read_bytes()[:300])
        with self.assertRaises(ValueError):
            verify_image(jpeg)
        bitmap = self.root / "image.bmp"
        Image.new("RGB", (8, 8)).save(bitmap, format="BMP")
        with self.assertRaises(ValueError):
            verify_image(bitmap)


if __name__ == "__main__":
    unittest.main()
