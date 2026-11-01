"""Offline checks for named workflow commands and parameter resolution."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "gemini-browser" / "scripts"))

import gemini_browser
import workflow_cli
import workflow_runner
from local_state import verify_image, write_json
from workflow_runner import workflow_record_path


class WorkflowCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="workflow-cli-test-")
        self.root = Path(self.temporary.name)
        self.registry = self.root / "workflows"
        self.output = self.root / "outputs"
        self.stack = ExitStack()
        self.emit = self.stack.enter_context(patch.object(workflow_cli, "emit"))
        self.browser = self.stack.enter_context(patch.object(gemini_browser, "BrowserSession", side_effect=AssertionError("offline command opened browser")))
        self.runner_browser = self.stack.enter_context(patch.object(workflow_runner, "BrowserSession", side_effect=AssertionError("offline command opened runner browser")))

    def tearDown(self):
        self.stack.close()
        self.temporary.cleanup()

    def command(self, *arguments, default_registry=False):
        common = ["--output-dir", str(self.output), "--data-dir", str(self.root / "private")]
        if not default_registry:
            common += ["--workflows-dir", str(self.registry)]
        self.emit.reset_mock()
        return gemini_browser.main(common + ["workflow", *arguments])

    def create(self, name="sample", *, default_registry=False):
        self.assertEqual(self.command("create", name, "--type", "image",
                                      "--prompt", "Draw {{subject}} in {{style}}", "--required", "subject",
                                      "--var", "style=watercolor", default_registry=default_registry), 0)

    def test_create_list_show_validate_plan_and_dry_run_never_open_browser(self):
        with patch.object(workflow_runner.WorkflowRunner, "run", side_effect=AssertionError("dry-run must not execute")) as execute:
            self.create()
            for arguments, event in [
                (("list",), "workflow_list"), (("show", "sample"), "workflow_definition"),
                (("validate", "sample"), "workflow_validated"),
                (("plan", "sample", "--set", "subject=cat"), "workflow_plan"),
                (("run", "sample", "--set", "subject=cat", "--dry-run"), "workflow_plan"),
            ]:
                with self.subTest(command=arguments):
                    self.assertEqual(self.command(*arguments), 0)
                    self.assertEqual(self.emit.call_args.args[0], event)
            execute.assert_not_called()
        self.browser.assert_not_called()
        self.runner_browser.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_params_file_and_set_override_are_rendered_before_execution(self):
        self.create()
        parameters = self.root / "parameters.json"
        write_json(parameters, {"subject": "file cat", "style": "ink"})
        self.assertEqual(self.command("plan", "sample", "--params-file", str(parameters),
                                      "--set", "subject=override cat"), 0)
        plan = self.emit.call_args.kwargs["plan"]
        self.assertEqual(plan["parameters"], {"subject": "override cat", "style": "ink"})
        self.assertEqual(plan["steps"][0]["prompt"], "Draw override cat in ink")
        self.browser.assert_not_called()

    def test_duplicate_set_assignments_are_rejected_without_browser(self):
        self.create()
        self.assertEqual(self.command("plan", "sample", "--set", "subject=cat", "--set", "subject=dog"), 1)
        self.assertEqual(self.emit.call_args.args[0], "error")
        self.assertIn("重复", self.emit.call_args.kwargs["message"])
        self.browser.assert_not_called()

    def test_missing_dry_run_parameter_is_reported_without_execution(self):
        self.create()
        with patch.object(workflow_runner.WorkflowRunner, "run") as execute:
            self.assertEqual(self.command("run", "sample", "--dry-run"), 1)
        execute.assert_not_called()
        self.assertIn("缺少必填参数", self.emit.call_args.kwargs["message"])
        self.browser.assert_not_called()

    def test_default_registry_is_current_working_directory(self):
        with patch.object(Path, "cwd", return_value=self.root):
            self.create(default_registry=True)
            self.assertEqual(self.command("list", default_registry=True), 0)
        self.assertTrue((self.root / "workflows" / "sample" / "workflow.json").is_file())
        self.assertEqual(self.emit.call_args.kwargs["directory"], str(self.registry))

    def test_result_checks_local_media_without_browser_or_execution(self):
        image = self.root / "saved.png"
        Image.new("RGB", (32, 24), "blue").save(image)
        run_id = "a" * 32
        record = {"id": run_id, "status": "succeeded", "steps": [
            {"id": "still", "type": "image", "files": [verify_image(image)]},
        ]}
        write_json(workflow_record_path(self.output, run_id), record)
        with patch.object(workflow_runner.WorkflowRunner, "run") as execute:
            self.assertEqual(self.command("result", run_id), 0)
        execute.assert_not_called()
        self.assertTrue(self.emit.call_args.kwargs["local_only"])
        self.assertEqual(self.emit.call_args.kwargs["run"], record)
        self.browser.assert_not_called()
        self.runner_browser.assert_not_called()

    def test_run_dispatch_passes_resolved_parameters_and_profile(self):
        self.create()
        with patch.object(workflow_runner, "WorkflowRunner") as runner:
            self.assertEqual(self.command("run", "sample", "--set", "subject=cat"), 0)
        runner.assert_called_once()
        self.assertEqual(runner.call_args.args[1:3], (self.output, self.root / "private" / "profiles" / "default"))
        runner.return_value.run.assert_called_once_with({"subject": "cat"})
        self.browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
