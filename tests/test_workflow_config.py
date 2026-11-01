"""Workflow schema, portable resources and interpolation, without browser access."""

from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / "gemini-browser" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from local_state import read_json, write_json
from workflow_config import WorkflowDefinition, WorkflowError, WorkflowStore


class WorkflowConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.image = self.source / "样例.png"
        Image.new("RGB", (40, 30), "orange").save(self.image)
        self.workflows = self.root / "workflows"
        self.store = WorkflowStore(self.workflows)

    def tearDown(self):
        self.temporary.cleanup()

    def spec(self):
        return {"schema_version": 1, "description": "固定样例图转图片再转视频",
                "variables": {"主题": None, "风格": "水彩"},
                "steps": [
                    {"id": "picture", "type": "image", "prompt": "画{{主题}}，采用{{ 风格 }}风格",
                     "references": ["样例.png"]},
                    {"id": "animate", "type": "video", "prompt": "让{{主题}}慢慢转头",
                     "references": [{"step": "picture"}]},
                ]}

    def create(self, spec=None, name="demo"):
        return self.store.create(name, self.spec() if spec is None else spec, self.source)

    def test_create_normalizes_resources_without_mutating_spec(self):
        spec = self.spec()
        before = copy.deepcopy(spec)
        result = self.create(spec)
        self.assertEqual(spec, before)
        manifest = read_json(result["manifest_path"])
        self.assertEqual(result["workflow"], manifest)
        self.assertEqual(manifest["name"], "demo")
        self.assertNotIn("prompt", manifest["steps"][0])
        self.assertEqual(manifest["steps"][0]["prompt_file"], "prompts/picture.txt")
        self.assertEqual(manifest["steps"][0]["references"], ["assets/picture-1.png"])
        self.assertEqual(manifest["steps"][1]["references"], [{"step": "picture", "file_index": 0}])
        summary = self.store.load("demo").validate()
        self.assertEqual(len(summary["steps"]), 2)
        self.assertTrue(summary["variables"]["主题"]["required"])
        self.assertEqual(summary["steps"][1]["timeout"], 600.0)

    def test_plan_renders_required_defaults_and_prior_image_reference(self):
        self.create()
        plan = self.store.load("demo").plan({"主题": "一只猫"})
        self.assertEqual(plan["parameters"], {"主题": "一只猫", "风格": "水彩"})
        self.assertEqual(plan["steps"][0]["prompt"], "画一只猫，采用水彩风格")
        self.assertEqual(plan["steps"][1]["references"], [{"step": "picture", "file_index": 0}])
        reference = plan["steps"][0]["references"][0]
        self.assertEqual((reference["width"], reference["height"]), (40, 30))
        self.assertTrue(Path(reference["path"]).is_relative_to(self.workflows / "demo"))

    def test_all_public_results_are_json_serializable(self):
        created = self.create()
        definition = self.store.load("demo")
        for result in [created, definition.validate(), definition.plan({"主题": "cat"}), self.store.list()]:
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
            self.assertTrue(encoded)

    def test_chinese_underscore_and_scalar_defaults_are_rendered_without_evaluation(self):
        spec = {"schema_version": 1, "variables": {"主题_1": "森林", "_风格": "水彩", "数量": 3,
                                                    "开启": True, "比例": 1.5},
                "steps": [{"id": "image", "type": "image",
                           "prompt": "{{主题_1}} {{_风格}} {{数量}} {{开启}} {{比例}}"}]}
        self.create(spec)
        prompt = self.store.load("demo").plan()["steps"][0]["prompt"]
        self.assertEqual(prompt, "森林 水彩 3 true 1.5")
        literal = "__import__('os').system('never executed')"
        plan = self.store.load("demo").plan({"主题_1": literal})
        self.assertTrue(plan["steps"][0]["prompt"].startswith(literal))

    def test_missing_unknown_null_composite_or_infinite_parameters_are_rejected(self):
        self.create()
        definition = self.store.load("demo")
        invalid = [{}, {"主题": "cat", "typo": "value"}, {"主题": None},
                   {"主题": []}, {"主题": {}}, {"主题": float("nan")}, {"主题": float("inf")}]
        for parameters in invalid:
            with self.subTest(parameters_type=[type(v).__name__ for v in parameters.values()]):
                with self.assertRaises(WorkflowError):
                    definition.plan(parameters)

    def test_unknown_or_malformed_template_is_rejected_before_writing(self):
        for template in ["{{未声明}}", "{{主题.foo}}", "{{主题", "{{}}", "{{{{主题}}}}"]:
            with self.subTest(template=template):
                spec = self.spec()
                spec["steps"][0]["prompt"] = template
                with self.assertRaises(WorkflowError):
                    self.create(spec)
                self.assertFalse(self.workflows.exists())

    def test_prompt_file_and_absolute_source_images_are_copied_and_portable(self):
        prompt = self.source / "prompt.txt"
        prompt.write_text("\ufeff保留样例，改成{{主题}}", encoding="utf-8")
        spec = self.spec()
        spec["steps"][0].pop("prompt")
        spec["steps"][0].update(prompt_file=str(prompt.resolve()), references=[str(self.image.resolve())])
        self.create(spec)
        relocated = self.root / "relocated" / "workflows"
        shutil.copytree(self.workflows / "demo", relocated / "demo")
        shutil.rmtree(self.source)
        shutil.rmtree(self.workflows)
        plan = WorkflowStore(relocated).load("demo").plan({"主题": "像素猫"})
        self.assertEqual(plan["steps"][0]["prompt"], "保留样例，改成像素猫")
        reference = Path(plan["steps"][0]["references"][0]["path"])
        self.assertTrue(reference.is_relative_to(relocated / "demo"))
        self.assertTrue(reference.is_file())

    def test_corrupt_image_or_non_utf8_prompt_leaves_no_new_directory(self):
        self.image.write_bytes(b"corrupt")
        with self.assertRaises(WorkflowError):
            self.create()
        self.assertFalse(self.workflows.exists())
        Image.new("RGB", (40, 30)).save(self.image)
        bad_prompt = self.source / "broken.txt"
        bad_prompt.write_bytes(b"\xff\xfe\xfd")
        spec = self.spec()
        spec["steps"][0].pop("prompt")
        spec["steps"][0]["prompt_file"] = "broken.txt"
        with self.assertRaises(WorkflowError):
            self.create(spec)
        self.assertFalse(self.workflows.exists())

    def test_create_does_not_overwrite_or_delete_existing_directory(self):
        existing = self.workflows / "demo"
        existing.mkdir(parents=True)
        sentinel = existing / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.create()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_copy_failure_removes_only_this_invocations_directory(self):
        sibling = self.workflows / "other"
        sibling.mkdir(parents=True)
        sentinel = sibling / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with patch("workflow_config.shutil.copyfile", side_effect=OSError("simulated copy failure")):
            with self.assertRaisesRegex(OSError, "simulated copy failure"):
                self.create()
        self.assertFalse((self.workflows / "demo").exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_changed_source_during_copy_is_rejected_and_cleaned(self):
        original_copy = shutil.copyfile

        def changed_copy(source, target):
            Image.new("RGB", (40, 30), "blue").save(source)
            return original_copy(source, target)

        with patch("workflow_config.shutil.copyfile", side_effect=changed_copy):
            with self.assertRaisesRegex(WorkflowError, "复制期间发生变化"):
                self.create()
        self.assertFalse((self.workflows / "demo").exists())

    def test_forward_self_video_and_negative_index_references_are_rejected(self):
        invalid_references = [{"step": "animate"}, {"step": "missing"},
                              {"step": "picture", "file_index": -1},
                              {"step": "picture", "file_index": True}]
        for reference in invalid_references:
            with self.subTest(reference=reference):
                spec = self.spec()
                spec["steps"][1]["references"] = [reference]
                with self.assertRaises(WorkflowError):
                    self.create(spec)
        spec = self.spec()
        spec["steps"].append({"id": "third", "type": "video", "prompt": "animate",
                              "references": [{"step": "animate"}]})
        with self.assertRaisesRegex(WorkflowError, "不能引用视频"):
            self.create(spec)
        spec = self.spec()
        spec["steps"][0]["references"] = [{"step": "animate"}]
        with self.assertRaisesRegex(WorkflowError, "前序步骤"):
            self.create(spec)

    def test_duplicate_ids_and_video_without_reference_are_rejected(self):
        spec = self.spec()
        spec["steps"][1]["id"] = "picture"
        with self.assertRaisesRegex(WorkflowError, "重复"):
            self.create(spec)
        spec = self.spec()
        spec["steps"][1].pop("references")
        with self.assertRaisesRegex(WorkflowError, "至少"):
            self.create(spec)

    def test_unknown_fields_are_rejected_at_every_schema_level(self):
        for target in ["root", "step", "reference"]:
            with self.subTest(target=target):
                spec = self.spec()
                selected = spec if target == "root" else spec["steps"][0] if target == "step" else spec["steps"][1]["references"][0]
                selected["typo"] = "value"
                with self.assertRaisesRegex(WorkflowError, "未知字段"):
                    self.create(spec)

    def test_schema_prompt_choice_variable_names_and_defaults_are_strict(self):
        variants = []
        for version in [True, 2, None]:
            spec = self.spec(); spec["schema_version"] = version; variants.append(spec)
        spec = self.spec(); spec["steps"] = []; variants.append(spec)
        spec = self.spec(); spec["steps"][0]["prompt_file"] = "anything.txt"; variants.append(spec)
        spec = self.spec(); spec["steps"][0].pop("prompt"); variants.append(spec)
        spec = self.spec(); spec["variables"]["bad.name"] = "x"; variants.append(spec)
        spec = self.spec(); spec["variables"]["主题"] = []; variants.append(spec)
        spec = self.spec(); spec["variables"]["主题"] = float("inf"); variants.append(spec)
        spec = self.spec(); spec["steps"][0]["type"] = []; variants.append(spec)
        for number, spec in enumerate(variants):
            with self.subTest(case=number):
                with self.assertRaises(WorkflowError):
                    self.create(spec)
        self.assertFalse(self.workflows.exists())

    def test_timeout_must_be_finite_positive_and_not_boolean(self):
        for timeout in [True, False, 0, -1, "300", float("inf"), float("nan")]:
            with self.subTest(timeout_type=type(timeout).__name__):
                spec = self.spec()
                spec["steps"][0]["timeout"] = timeout
                with self.assertRaisesRegex(WorkflowError, "有限正数"):
                    self.create(spec)
        spec = self.spec()
        spec["steps"][0]["timeout"] = 0.5
        self.create(spec)
        self.assertEqual(self.store.load("demo").plan({"主题": "cat"})["steps"][0]["timeout"], 0.5)

    def test_persistent_reference_or_prompt_cannot_escape_bundle(self):
        self.create()
        original = self.store.load("demo")
        for path in ["../outside.png", "assets/../../outside.png", "C:\\outside.png", "/outside.png"]:
            for field in ["references", "prompt_file"]:
                with self.subTest(path=path, field=field):
                    manifest = copy.deepcopy(original.manifest)
                    manifest["steps"][0][field] = [path] if field == "references" else path
                    definition = WorkflowDefinition("demo", original.directory, manifest)
                    with self.assertRaises(WorkflowError):
                        definition.plan({"主题": "cat"})
                    write_json(original.directory / "workflow.json", manifest)
                    with self.assertRaises(WorkflowError):
                        self.store.load("demo")
        write_json(original.directory / "workflow.json", original.manifest)

    def test_symlink_resource_outside_bundle_is_rejected(self):
        self.create()
        definition = self.store.load("demo")
        asset = definition.directory / definition.manifest["steps"][0]["references"][0]
        asset.unlink()
        try:
            asset.symlink_to(self.image)
        except OSError:
            # Windows may deny creating a symlink without developer mode. Still
            # exercise the canonical-path boundary using its resolved outcome.
            original_resolve = Path.resolve
            outside = self.image.resolve()

            def resolved(path, *args, **kwargs):
                return outside if path == asset else original_resolve(path, *args, **kwargs)

            with patch.object(Path, "resolve", autospec=True, side_effect=resolved):
                with self.assertRaisesRegex(WorkflowError, "符号链接"):
                    definition.plan({"主题": "cat"})
            return
        with self.assertRaisesRegex(WorkflowError, "符号链接"):
            definition.plan({"主题": "cat"})

    def test_name_and_collection_symlink_boundaries_are_enforced(self):
        for name in ["../escape", "a/b", "", "工作流", "x" * 65]:
            with self.subTest(name=name):
                with self.assertRaises(WorkflowError):
                    self.create(name=name)
        self.create(name="_demo")
        self.assertEqual(self.store.load("_demo").name, "_demo")

    def test_windows_reserved_workflow_and_step_names_are_rejected(self):
        for reserved in ["CON", "prn", "Aux", "NUL", "COM1", "COM9", "LPT1", "lpt9"]:
            with self.subTest(name=reserved):
                with self.assertRaisesRegex(WorkflowError, "Windows 保留名称"):
                    self.create(name=reserved)
                spec = self.spec()
                spec["steps"][0]["id"] = reserved
                with self.assertRaisesRegex(WorkflowError, "Windows 保留名称"):
                    self.create(spec)
        self.assertFalse(self.workflows.exists())

    def test_list_load_and_validation_summary(self):
        self.assertEqual(self.store.list(), [])
        self.create(name="z-demo")
        self.create(name="a-demo")
        listed = self.store.list()
        self.assertEqual([entry["name"] for entry in listed], ["a-demo", "z-demo"])
        self.assertTrue(all(entry["description"] for entry in listed))
        with self.assertRaisesRegex(WorkflowError, "清单不存在"):
            self.store.load("missing")
        bad = self.workflows / "broken"
        bad.mkdir()
        (bad / "workflow.json").write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "UTF-8 JSON"):
            self.store.load("broken")


if __name__ == "__main__":
    unittest.main()
