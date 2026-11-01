"""Offline CLI tests: local results, isolated generation, and chat cleanup."""

from __future__ import annotations

import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / "gemini-browser" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import gemini_browser
from local_state import RunStore, verify_image


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "data"
        self.output_dir = self.root / "outputs"
        self.profile = self.data_dir / "profiles" / "default"
        self.options = ["--data-dir", str(self.data_dir), "--output-dir", str(self.output_dir)]

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, arguments, session_factory=None, driver_class=None):
        factory = session_factory or MagicMock()
        driver = driver_class or MagicMock()
        module = types.ModuleType("gemini_web")
        module.GeminiImages = driver
        with patch.object(gemini_browser, "BrowserSession", factory), \
             patch.object(gemini_browser, "emit") as events, \
             patch.dict(sys.modules, {"gemini_web": module}), \
             patch("sys.stdout", new=io.StringIO()):
            result = gemini_browser.main(arguments)
        return result, factory, driver, events

    def test_common_options_work_before_or_after_command(self):
        for command in ["login", "status", "image", "video", "result", "cleanup"]:
            extra = {"login": [], "status": [], "image": ["--prompt", "一只猫"],
                     "video": ["--prompt", "一只猫动起来", "--reference", "cat.png"],
                     "result": ["run123"], "cleanup": ["run123"]}[command]
            supplied = ["--profile", "my-profile", "--proxy", "http://127.0.0.1:7897", *self.options]
            with self.subTest(command=command, position="before"):
                args = gemini_browser.parser_for_cli().parse_args([*supplied, command, *extra])
                self.assertEqual(args.profile, "my-profile")
                self.assertEqual(args.data_dir, self.data_dir)
                self.assertEqual(args.output_dir, self.output_dir)
                self.assertEqual(args.proxy, "http://127.0.0.1:7897")
            with self.subTest(command=command, position="after"):
                args = gemini_browser.parser_for_cli().parse_args([command, *extra, *supplied])
                self.assertEqual(args.profile, "my-profile")
                self.assertEqual(args.data_dir, self.data_dir)
                self.assertEqual(args.output_dir, self.output_dir)
                self.assertEqual(args.proxy, "http://127.0.0.1:7897")

    def saved_image_record(self, *, status="succeeded", profile=None):
        image = self.root / "cat.png"
        Image.new("RGB", (12, 16)).save(image)
        store = RunStore(self.output_dir)
        record = store.create("a cat", profile or self.profile)
        record.update(status=status, files=[verify_image(image)],
                      chat_url="https://gemini.google.com/app/abc123")
        store.save(record)
        return record

    def test_resume_command_has_been_removed(self):
        with patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                gemini_browser.parser_for_cli().parse_args(["resume", "old-run"])
        self.assertEqual(raised.exception.code, 2)

    def test_result_validates_local_files_without_profile_or_browser(self):
        record = self.saved_image_record(status="failed", profile=self.root / "other-profile")
        result, factory, driver, events = self.invoke([*self.options, "result", record["id"]])
        self.assertEqual(result, 0)
        factory.assert_not_called()
        driver.assert_not_called()
        self.assertEqual(events.call_args.args, ("result",))
        self.assertTrue(events.call_args.kwargs["local_only"])
        self.assertEqual(RunStore(self.output_dir).load(record["id"]), record)

    def test_result_without_files_fails_without_browser_or_record_mutation(self):
        record = RunStore(self.output_dir).create("a cat", self.profile)
        result, factory, driver, events = self.invoke([*self.options, "result", record["id"]])
        self.assertEqual(result, 1)
        factory.assert_not_called()
        driver.assert_not_called()
        self.assertIn("没有已保存的本地图片", events.call_args.kwargs["message"])
        self.assertEqual(RunStore(self.output_dir).load(record["id"]), record)

    def test_result_rejects_corrupt_file_without_opening_browser(self):
        record = self.saved_image_record()
        Path(record["files"][0]["path"]).write_bytes(b"corrupt")
        result, factory, driver, _ = self.invoke([*self.options, "result", record["id"]])
        self.assertEqual(result, 1)
        factory.assert_not_called()
        driver.assert_not_called()
        self.assertEqual(RunStore(self.output_dir).load(record["id"]), record)

    def test_cleanup_only_calls_cleanup_without_generating(self):
        record = self.saved_image_record()
        driver = MagicMock()

        def clean(record):
            record["chat_cleanup"] = {"status": "deleted"}
            RunStore(self.output_dir).save(record)

        driver.return_value.cleanup.side_effect = clean
        result, factory, driver, events = self.invoke(
            [*self.options, "cleanup", record["id"]], driver_class=driver,
        )
        self.assertEqual(result, 0)
        driver.return_value.cleanup.assert_called_once()
        driver.return_value.generate.assert_not_called()
        driver.return_value.resume.assert_not_called()
        factory.return_value.__exit__.assert_called_once()

    def test_image_reference_argument_is_optional_and_repeatable(self):
        parser = gemini_browser.parser_for_cli()
        plain = parser.parse_args(["image", "--prompt", "a cat"])
        self.assertEqual(plain.reference, [])
        self.assertEqual(plain.timeout, 300)
        referenced = parser.parse_args([
            "image", "--prompt", "a cat", "--reference", "one.png", "--reference", "two.jpg",
        ])
        self.assertEqual(referenced.reference, [Path("one.png"), Path("two.jpg")])

    def test_video_requires_reference_and_uses_600_second_default(self):
        parser = gemini_browser.parser_for_cli()
        with patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                parser.parse_args(["video", "--prompt", "a cat moving"])
        self.assertEqual(raised.exception.code, 2)
        args = parser.parse_args([
            "video", "--prompt-file", "motion.txt", "--reference", "cat.png",
            "--reference", "background.png",
        ])
        self.assertEqual(args.timeout, 600)
        self.assertEqual(args.prompt_file, Path("motion.txt"))
        self.assertEqual(args.reference, [Path("cat.png"), Path("background.png")])

    def test_invalid_references_never_open_browser_or_create_task(self):
        corrupt = self.root / "broken.png"
        corrupt.write_bytes(b"not a reference image")
        for command in ["image", "video"]:
            for path in [self.root / "missing.png", corrupt]:
                with self.subTest(command=command, path=path.name):
                    result, factory, driver, _ = self.invoke([
                        *self.options, command, "--prompt", "a cat", "--reference", str(path),
                    ])
                    self.assertEqual(result, 1)
                    factory.assert_not_called()
                    driver.assert_not_called()
                    self.assertFalse(self.output_dir.exists())

    def test_valid_references_and_media_type_are_persisted_before_generation(self):
        reference = self.root / "reference.png"
        Image.new("RGB", (32, 48), "green").save(reference)
        for command, timeout in [("image", 300), ("video", 600)]:
            with self.subTest(command=command):
                result, _, driver, _ = self.invoke([
                    *self.options, command, "--prompt", "a cat", "--reference", str(reference),
                ])
                self.assertEqual(result, 0)
                record, passed_timeout = driver.return_value.generate.call_args.args
                self.assertEqual(passed_timeout, timeout)
                self.assertEqual(record["media_type"], command)
                self.assertEqual(record["references"], [verify_image(reference)])
                self.assertTrue(Path(record["references"][0]["path"]).is_absolute())
                persisted = RunStore(self.output_dir).load(record["id"])
                self.assertEqual(persisted["media_type"], command)
                self.assertEqual(persisted["references"], record["references"])

    def test_text_to_image_keeps_empty_references_and_image_type(self):
        result, _, driver, _ = self.invoke([*self.options, "image", "--prompt", "a cat"])
        self.assertEqual(result, 0)
        record, _ = driver.return_value.generate.call_args.args
        self.assertEqual(record["media_type"], "image")
        self.assertEqual(record["references"], [])

    def test_result_dispatches_video_validation_locally(self):
        path = self.root / "clip.mp4"
        path.write_bytes(b"mock only; validation dispatch tested separately")
        store = RunStore(self.output_dir)
        record = store.create("a moving cat", self.profile)
        record.update(status="succeeded", media_type="video", files=[{"path": str(path)}],
                      chat_cleanup={"status": "deleted"})
        store.save(record)
        metadata = {"path": str(path), "media_type": "video", "format": "MP4"}
        with patch("local_state.verify_media", return_value=metadata, create=True) as verify:
            result, factory, driver, events = self.invoke([*self.options, "result", record["id"]])
        self.assertEqual(result, 0)
        verify.assert_called_once_with(str(path), "video")
        factory.assert_not_called()
        driver.assert_not_called()
        self.assertEqual(events.call_args.kwargs["files"], [metadata])
        self.assertEqual(events.call_args.kwargs["media_type"], "video")

    def test_video_cleanup_failure_preserves_generated_video_metadata(self):
        driver = MagicMock()
        reference = self.root / "reference.png"
        Image.new("RGB", (24, 32)).save(reference)

        def fail_during_cleanup(record, timeout):
            path = self.root / "saved-video.mp4"
            path.write_bytes(b"mock generated video")
            record.update(status="succeeded", files=[{"path": str(path), "media_type": "video"}],
                          chat_cleanup={"status": "failed"})
            RunStore(self.output_dir).save(record)
            raise RuntimeError("mock video saved but deletion failed")

        driver.return_value.generate.side_effect = fail_during_cleanup
        result, _, _, events = self.invoke([
            *self.options, "video", "--prompt", "a moving cat", "--reference", str(reference),
        ], driver_class=driver)
        self.assertEqual(result, 1)
        run_id = events.call_args.kwargs["run_id"]
        restored = RunStore(self.output_dir).load(run_id)
        self.assertEqual(restored["status"], "succeeded")
        self.assertEqual(restored["media_type"], "video")
        self.assertEqual(restored["chat_cleanup"]["status"], "failed")
        self.assertEqual(Path(restored["files"][0]["path"]).read_bytes(), b"mock generated video")
        self.assertFalse(any(call.args[0] == "succeeded" for call in events.call_args_list))

    def test_cleanup_completed_states_return_locally_without_redeleting(self):
        for state in ["deleted", "not_created"]:
            with self.subTest(state=state):
                record = self.saved_image_record(profile=self.root / "different-profile")
                record.update(chat_url=None, chat_cleanup={"status": state})
                RunStore(self.output_dir).save(record)
                result, factory, driver, events = self.invoke([*self.options, "cleanup", record["id"]])
                self.assertEqual(result, 0)
                factory.assert_not_called()
                driver.assert_not_called()
                self.assertTrue(events.call_args.kwargs["already_cleaned"])

    def test_cleanup_missing_chat_url_does_not_open_browser(self):
        record = self.saved_image_record()
        record["chat_url"] = None
        RunStore(self.output_dir).save(record)
        result, factory, driver, _ = self.invoke([*self.options, "cleanup", record["id"]])
        self.assertEqual(result, 1)
        factory.assert_not_called()
        driver.assert_not_called()
        restored = RunStore(self.output_dir).load(record["id"])
        self.assertEqual(restored["status"], "succeeded")
        self.assertEqual(restored["files"], record["files"])
        self.assertEqual(restored["chat_cleanup"]["status"], "failed")

    def test_cleanup_mismatched_profile_does_not_open_browser(self):
        record = self.saved_image_record(profile=self.root / "other-profile")
        result, factory, _, _ = self.invoke([*self.options, "cleanup", record["id"]])
        self.assertEqual(result, 1)
        factory.assert_not_called()

    def test_cleanup_failure_preserves_saved_image_and_generation_status(self):
        record = self.saved_image_record()
        driver = MagicMock()
        driver.return_value.cleanup.side_effect = RuntimeError("mock delete failed")
        result, factory, _, _ = self.invoke(
            [*self.options, "cleanup", record["id"]], driver_class=driver,
        )
        self.assertEqual(result, 1)
        factory.return_value.__exit__.assert_called_once()
        restored = RunStore(self.output_dir).load(record["id"])
        self.assertEqual(restored["status"], "succeeded")
        self.assertEqual(restored["files"], record["files"])
        self.assertEqual(verify_image(restored["files"][0]["path"])["sha256"], record["files"][0]["sha256"])
        self.assertEqual(restored["chat_cleanup"]["status"], "failed")

    def test_cleanup_without_deleted_confirmation_is_failure(self):
        record = self.saved_image_record()
        result, _, _, _ = self.invoke([*self.options, "cleanup", record["id"]])
        self.assertEqual(result, 1)
        restored = RunStore(self.output_dir).load(record["id"])
        self.assertEqual(restored["status"], "succeeded")
        self.assertEqual(restored["chat_cleanup"]["status"], "failed")

    def test_cleanup_interruption_is_nonzero_and_does_not_suggest_resume(self):
        record = self.saved_image_record()
        driver = MagicMock()
        driver.return_value.cleanup.side_effect = KeyboardInterrupt()
        result, _, _, events = self.invoke(
            [*self.options, "cleanup", record["id"]], driver_class=driver,
        )
        self.assertEqual(result, 130)
        self.assertNotIn("resume", events.call_args.kwargs["message"])
        restored = RunStore(self.output_dir).load(record["id"])
        self.assertEqual(restored["status"], "succeeded")
        self.assertEqual(restored["files"], record["files"])
        self.assertEqual(restored["chat_cleanup"]["status"], "failed")

    def test_image_does_not_duplicate_driver_success_or_cleanup(self):
        result, _, driver, events = self.invoke([*self.options, "image", "--prompt", "一只猫"])
        self.assertEqual(result, 0)
        driver.return_value.generate.assert_called_once()
        driver.return_value.cleanup.assert_not_called()
        self.assertFalse(any(call.args[0] == "succeeded" for call in events.call_args_list))

    def test_generation_cleanup_failure_preserves_local_success(self):
        driver = MagicMock()

        def fail_during_cleanup(record, timeout):
            image = self.root / "generated.png"
            Image.new("RGB", (24, 32)).save(image)
            record.update(status="succeeded", files=[verify_image(image)], chat_cleanup={"status": "failed"})
            RunStore(self.output_dir).save(record)
            raise RuntimeError("mock image saved but deletion failed")

        driver.return_value.generate.side_effect = fail_during_cleanup
        result, _, _, events = self.invoke(
            [*self.options, "image", "--prompt", "一只猫"], driver_class=driver,
        )
        self.assertEqual(result, 1)
        run_id = events.call_args.kwargs["run_id"]
        restored = RunStore(self.output_dir).load(run_id)
        self.assertEqual(restored["status"], "succeeded")
        self.assertEqual(restored["chat_cleanup"]["status"], "failed")
        self.assertEqual(len(restored["files"]), 1)

    def test_generation_failure_after_submit_preserves_uncertain_status(self):
        driver = MagicMock()

        def fail_after_submit(record, timeout):
            record.update(status="submitted", chat_url="https://gemini.google.com/app/abc123")
            RunStore(self.output_dir).save(record)
            raise RuntimeError("mock interrupted after submission")

        driver.return_value.generate.side_effect = fail_after_submit
        result, factory, _, events = self.invoke(
            [*self.options, "image", "--prompt", "一只猫"], driver_class=driver,
        )
        self.assertEqual(result, 1)
        run_id = events.call_args.kwargs["run_id"]
        self.assertEqual(RunStore(self.output_dir).load(run_id)["status"], "submitted")
        driver.return_value.generate.assert_called_once()
        factory.return_value.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
