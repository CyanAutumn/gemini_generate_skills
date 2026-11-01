"""Workflow execution invariants with real files and a browser-free driver."""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "gemini-browser" / "scripts"))

import workflow_runner
from browser_smoke import video_fixture_bytes
from local_state import read_json, verify_media
from workflow_config import WorkflowError, WorkflowStore
from workflow_runner import WorkflowRunner, load_workflow_result, workflow_record_path


class WorkflowRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="workflow-runner-test-")
        self.root = Path(self.temporary.name)
        self.output = self.root / "outputs"
        self.profile = self.root / "profile"
        self.source = self.root / "source.png"
        Image.new("RGB", (32, 24), "orange").save(self.source)
        self.spec = {
            "schema_version": 1, "name": "pipeline", "variables": {"subject": None},
            "steps": [
                {"id": "still", "type": "image", "prompt": "Draw {{subject}}", "references": ["source.png"]},
                {"id": "motion", "type": "video", "prompt": "Animate {{subject}}", "references": [{"step": "still"}]},
            ],
        }
        self.registry = WorkflowStore(self.root / "workflows")
        self.registry.create("pipeline", self.spec, self.root)
        self.definition = self.registry.load("pipeline")
        self.session = MagicMock()
        self.session.__enter__.return_value = self.session
        self.session_factory = MagicMock(return_value=self.session)
        self.calls = []
        self.actions = {}
        self.video = video_fixture_bytes()
        owner = self

        class FileDriver:
            def __init__(self, session, store):
                self.store = store

            def generate(self, task, timeout):
                owner.calls.append(copy.deepcopy(task))
                action = owner.actions.get(task["workflow_step"])
                if isinstance(action, BaseException):
                    task.update(status="interrupted" if isinstance(action, KeyboardInterrupt) else "failed",
                                chat_cleanup={"status": "deleted"})
                    self.store.save(task)
                    raise action
                destination = self.store.path_for(task["id"]).parent / ("output.mp4" if task["media_type"] == "video" else "output.png")
                if task["media_type"] == "video":
                    destination.write_bytes(owner.video)
                else:
                    Image.new("RGB", (40, 30), "blue").save(destination)
                if action == "invalid_media":
                    destination.write_bytes(b"<html>not a generated image</html>")
                    files = [{"path": str(destination)}]
                else:
                    files = [verify_media(destination, task["media_type"])]
                if action == "wrong_hash":
                    files[0]["sha256"] = "0" * 64
                task.update(status="succeeded", files=files,
                            chat_cleanup={"status": "failed" if action == "cleanup_failed" else "deleted"})
                self.store.save(task)

        self.driver_factory = MagicMock(side_effect=FileDriver)
        self.runner = WorkflowRunner(self.definition, self.output, self.profile,
                                     session_factory=self.session_factory, driver_factory=self.driver_factory)
        self.events = patch.object(workflow_runner, "emit")
        self.emit = self.events.start()

    def tearDown(self):
        self.events.stop()
        self.temporary.cleanup()

    def assert_no_browser_or_output(self):
        self.session_factory.assert_not_called()
        self.driver_factory.assert_not_called()
        self.assertIsNone(self.runner.last_record)
        self.assertFalse(self.output.exists())

    def test_missing_parameter_fails_before_browser_or_output_creation(self):
        with self.assertRaisesRegex(WorkflowError, "缺少必填参数"):
            self.runner.run()
        self.assert_no_browser_or_output()

    def test_unknown_parameter_fails_before_browser_or_output_creation(self):
        with self.assertRaisesRegex(WorkflowError, "未声明参数"):
            self.runner.run({"subject": "cat", "unexpected": "value"})
        self.assert_no_browser_or_output()

    def test_missing_fixed_reference_is_preflighted_before_browser(self):
        (self.definition.directory / "assets" / "still-1.png").unlink()
        with self.assertRaisesRegex(WorkflowError, "文件不存在"):
            self.runner.run({"subject": "cat"})
        self.assert_no_browser_or_output()

    def test_image_output_becomes_video_reference_in_one_session_and_unique_records(self):
        result = self.runner.run({"subject": "cat"})
        self.assertEqual(result["status"], "succeeded")
        self.session_factory.assert_called_once_with(self.profile, proxy=None)
        self.session.__enter__.assert_called_once()
        self.session.__exit__.assert_called_once()
        self.assertEqual(len(self.calls), 2)
        first, second = result["steps"]
        self.assertNotEqual(first["task_run_id"], second["task_run_id"])
        self.assertNotEqual(first["task_record"], second["task_record"])
        self.assertEqual(self.calls[1]["references"][0]["sha256"], first["files"][0]["sha256"])
        self.assertEqual(self.calls[1]["references"][0]["path"], first["files"][0]["path"])
        self.assertEqual(self.calls[1]["media_type"], "video")
        self.assertEqual(self.calls[1]["prompt"], "Animate cat")
        for step in result["steps"]:
            task = read_json(step["task_record"])
            self.assertEqual(task["workflow_run_id"], result["id"])
            self.assertEqual(task["workflow_step"], step["id"])
            self.assertEqual(step["chat_cleanup"]["status"], "deleted")
            verify_media(step["files"][0]["path"], step["type"])
        self.assertEqual(result["files"][0]["format"], "MP4")
        self.assertEqual(read_json(workflow_record_path(self.output, result["id"])), result)
        self.assertEqual([event.args[0] for event in self.emit.call_args_list].count("workflow_started"), 1)
        self.assertEqual([event.args[0] for event in self.emit.call_args_list].count("workflow_step_started"), 2)
        self.assertEqual(self.emit.call_args_list[-1].args[0], "workflow_succeeded")

    def test_driver_failure_is_not_retried_and_retains_prior_output(self):
        failure = RuntimeError("mock video generation failed")
        self.actions["motion"] = failure
        with self.assertRaises(RuntimeError) as raised:
            self.runner.run({"subject": "cat"})
        self.assertIs(raised.exception, failure)
        self.assertEqual([call["workflow_step"] for call in self.calls], ["still", "motion"])
        result = self.runner.last_record
        self.assertEqual(result["status"], "failed")
        self.assertEqual([step["status"] for step in result["steps"]], ["succeeded", "failed"])
        first_file = Path(result["steps"][0]["files"][0]["path"])
        self.assertTrue(first_file.is_file())
        self.assertEqual(read_json(workflow_record_path(self.output, result["id"]))["status"], "failed")

    def test_cleanup_failure_stops_before_next_step_and_retains_generated_file(self):
        self.actions["still"] = "cleanup_failed"
        with self.assertRaisesRegex(RuntimeError, "聊天尚未确认删除"):
            self.runner.run({"subject": "cat"})
        self.assertEqual(len(self.calls), 1)
        entry = self.runner.last_record["steps"][0]
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["chat_cleanup"]["status"], "failed")
        self.assertTrue(Path(entry["files"][0]["path"]).is_file())

    def test_invalid_or_changed_output_cannot_advance_to_next_step(self):
        for action in ("invalid_media", "wrong_hash"):
            with self.subTest(action=action):
                self.calls.clear()
                self.actions["still"] = action
                with self.assertRaises(ValueError):
                    self.runner.run({"subject": "cat"})
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.runner.last_record["status"], "failed")
                self.assertTrue(Path(self.runner.last_record["steps"][0]["files"][0]["path"]).is_file())

    def test_keyboard_interrupt_is_saved_and_original_interrupt_is_raised(self):
        interruption = KeyboardInterrupt()
        self.actions["motion"] = interruption
        with self.assertRaises(KeyboardInterrupt) as raised:
            self.runner.run({"subject": "cat"})
        self.assertIs(raised.exception, interruption)
        self.assertEqual(len(self.calls), 2)
        result = self.runner.last_record
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["steps"][1]["status"], "interrupted")
        self.assertEqual(read_json(workflow_record_path(self.output, result["id"]))["status"], "interrupted")
        self.assertTrue(Path(result["steps"][0]["files"][0]["path"]).is_file())

    def test_result_verification_is_local_and_detects_file_changes(self):
        result = self.runner.run({"subject": "cat"})
        self.session_factory.reset_mock()
        with patch.object(workflow_runner, "BrowserSession", side_effect=AssertionError("must remain local")):
            loaded = load_workflow_result(self.output, result["id"])
        self.assertEqual(loaded, result)
        self.session_factory.assert_not_called()
        file = Path(result["steps"][0]["files"][0]["path"])
        Image.new("RGB", (40, 30), "red").save(file)
        with self.assertRaisesRegex(ValueError, "本地文件已改变"):
            load_workflow_result(self.output, result["id"])


if __name__ == "__main__":
    unittest.main()
