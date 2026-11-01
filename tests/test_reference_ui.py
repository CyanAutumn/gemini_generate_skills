"""Offline reference-upload and video UI invariants; no account or browser."""

from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / "gemini-browser" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import gemini_web
from local_state import RunStore, verify_image


class ReferenceUITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = RunStore(self.root / "outputs")
        self.record = self.store.create("让参考图中的猫缓慢转头", self.root / "profile")
        self.reference = self.root / "reference.png"
        Image.new("RGB", (32, 48), "green").save(self.reference)
        self.record.update(media_type="image", references=[verify_image(self.reference)])
        self.session = MagicMock()
        self.page = self.session.page
        self.page.url = "https://gemini.google.com/app"
        self.page.locator.return_value.count.return_value = 0
        self.session.auth_state.return_value = "authenticated"
        self.session.diagnostics.return_value = {}
        self.driver = gemini_web.GeminiImages(self.session, self.store)
        self.emitter = patch.object(gemini_web, "emit")
        self.emit = self.emitter.start()

    def tearDown(self):
        self.emitter.stop()
        self.temporary.cleanup()

    def generation_stubs(self, *, upload=None, wait=None, cleanup=None, ready=None):
        stack = ExitStack()
        stubs = {"editor": MagicMock(), "send": MagicMock()}

        def new_chat(record):
            record.update(new_chat_verified=True, chat_cleanup={"status": "pending"})

        def deleted(record):
            record["chat_cleanup"] = {"status": "deleted"}
            self.store.save(record)

        stack.enter_context(patch.object(self.driver, "_new_chat", side_effect=new_chat))
        stack.enter_context(patch.object(self.driver, "_editor", return_value=stubs["editor"]))
        stubs["image_mode"] = stack.enter_context(patch.object(self.driver, "_select_image_mode", return_value="image_button"))
        stubs["video_mode"] = stack.enter_context(patch.object(self.driver, "_select_video_mode", return_value="video_button"))
        stubs["upload"] = stack.enter_context(patch.object(self.driver, "_upload_references", side_effect=upload))
        stack.enter_context(patch.object(self.driver, "_image_sources", return_value=set()))
        stack.enter_context(patch.object(self.driver, "_remember_url"))
        stack.enter_context(patch.object(self.driver, "_control", return_value=stubs["send"]))
        stubs["ready"] = stack.enter_context(patch.object(self.driver, "_ready_send", side_effect=ready, return_value=stubs["send"]))
        stubs["wait"] = stack.enter_context(patch.object(self.driver, "_wait_and_download", side_effect=wait))
        stubs["cleanup"] = stack.enter_context(patch.object(self.driver, "cleanup", side_effect=cleanup or deleted))
        return stack, stubs

    def test_no_references_does_not_open_upload_controls(self):
        self.record["references"] = []
        self.driver._upload_references(self.record)
        self.page.locator.assert_not_called()
        self.page.expect_file_chooser.assert_not_called()

    def test_plain_image_generation_uses_original_mode_and_no_upload_ready_gate(self):
        self.record["references"] = []

        def downloaded(record, *_):
            record.update(status="succeeded", files=[{"path": "image.png"}])

        stack, stubs = self.generation_stubs(wait=downloaded)
        with stack:
            self.driver.generate(self.record, 1)
        stubs["image_mode"].assert_called_once()
        stubs["video_mode"].assert_not_called()
        stubs["ready"].assert_not_called()
        stubs["send"].click.assert_called_once()

    def test_corrupt_or_changed_reference_is_rejected_before_any_upload(self):
        original = self.record["references"][0]
        for changed_to_valid_image in [False, True]:
            with self.subTest(valid_but_changed=changed_to_valid_image):
                self.record["references"] = [original]
                if changed_to_valid_image:
                    Image.new("RGB", (32, 48), "blue").save(self.reference)
                else:
                    self.reference.write_bytes(b"corrupt image")
                with self.assertRaises(ValueError):
                    self.driver._upload_references(self.record)
                self.page.locator.assert_not_called()
                self.page.expect_file_chooser.assert_not_called()

    def test_upload_without_ready_preview_times_out(self):
        inputs = MagicMock()
        inputs.count.return_value = 1
        images = MagicMock()
        images.all.return_value = []
        self.page.locator.side_effect = lambda selector: inputs if selector == 'input[type="file"]' else images
        with patch.object(self.driver, "_image_sources", return_value=set()), \
             patch.object(gemini_web.time, "monotonic", side_effect=[0, 0, 121]):
            with self.assertRaisesRegex(RuntimeError, "预览就绪"):
                self.driver._upload_references(self.record)
        inputs.first.set_input_files.assert_called_once()
        self.assertFalse(self.record.get("references_uploaded", False))

    def test_lost_first_menu_click_reopens_fresh_control_then_uploads_without_submitting(self):
        state = {"open": False, "uploaded": False}
        menu = MagicMock()
        menu.is_visible.side_effect = lambda: state["open"]
        self.page.get_by_role.return_value.all.return_value = [menu]
        first_toolbar, replacement_toolbar, upload, chooser = [MagicMock() for _ in range(4)]
        replacement_toolbar.click.side_effect = lambda: state.update(open=True)
        toolbar_controls = iter([first_toolbar, replacement_toolbar])
        pending = MagicMock()
        pending.value = chooser
        self.page.expect_file_chooser.return_value.__enter__.return_value = pending
        chooser.set_files.side_effect = lambda paths: state.update(uploaded=True)

        def control(pattern, scope=None):
            if pattern is gemini_web.UPLOAD_FILES:
                return upload if state["open"] else None
            if pattern is gemini_web.ADD_FILES:
                return next(toolbar_controls)
            raise AssertionError("上传方法不得寻找发送入口")

        with patch.object(self.driver, "_control", side_effect=control), \
             patch.object(self.driver, "_wait_control", side_effect=lambda *args: upload if state["open"] else None) as wait, \
             patch.object(self.driver, "_reference_previews_ready", side_effect=lambda count: state["uploaded"]), \
             patch.object(self.driver, "_ready_send") as send, \
             patch.object(self.driver, "_editor") as editor:
            self.driver._upload_references(self.record)
        first_toolbar.click.assert_called_once()
        replacement_toolbar.click.assert_called_once()
        self.assertEqual(wait.call_count, 2)
        upload.click.assert_called_once()
        chooser.set_files.assert_called_once_with([str(self.reference.resolve())])
        self.assertTrue(self.record["references_uploaded"])
        self.assertFalse(self.record.get("submission_attempted", False))
        send.assert_not_called()
        editor.assert_not_called()
        self.page.keyboard.press.assert_not_called()

    def test_visible_upload_menu_is_waited_for_without_toggling_it_closed(self):
        menu = MagicMock()
        menu.is_visible.return_value = True
        self.page.get_by_role.return_value.all.return_value = [menu]
        upload = MagicMock()
        chooser = MagicMock()
        pending = MagicMock()
        pending.value = chooser
        self.page.expect_file_chooser.return_value.__enter__.return_value = pending

        def only_existing_upload(pattern, scope=None):
            if pattern is not gemini_web.UPLOAD_FILES:
                raise AssertionError("菜单已经打开，不得重新点击添加文件或发送")
            return None

        with patch.object(self.driver, "_control", side_effect=only_existing_upload), \
             patch.object(self.driver, "_wait_control", return_value=upload) as wait, \
             patch.object(self.driver, "_reference_previews_ready", return_value=True):
            self.driver._upload_references(self.record)
        wait.assert_called_once_with(gemini_web.UPLOAD_FILES, 10)
        upload.click.assert_called_once()
        chooser.set_files.assert_called_once()
        self.assertTrue(self.record["references_uploaded"])
        self.assertFalse(self.record.get("submission_attempted", False))

    def test_upload_failure_never_reaches_prompt_or_submission_and_still_cleans(self):
        stack, stubs = self.generation_stubs(upload=RuntimeError("upload preview not ready"))
        with stack, self.assertRaisesRegex(RuntimeError, "preview not ready"):
            self.driver.generate(self.record, 1)
        stubs["editor"].fill.assert_not_called()
        stubs["send"].click.assert_not_called()
        stubs["editor"].press.assert_not_called()
        stubs["wait"].assert_not_called()
        stubs["cleanup"].assert_called_once()
        self.assertFalse(self.record.get("submission_attempted", False))

    def test_disabled_send_never_submits(self):
        stack, stubs = self.generation_stubs(ready=RuntimeError("send not ready"))
        with stack, self.assertRaisesRegex(RuntimeError, "send not ready"):
            self.driver.generate(self.record, 1)
        stubs["send"].click.assert_not_called()
        stubs["editor"].press.assert_not_called()
        stubs["cleanup"].assert_called_once()
        self.assertFalse(self.record.get("submission_attempted", False))

    def test_ready_send_requires_enabled_control(self):
        send = MagicMock()
        send.is_enabled.return_value = False
        with patch.object(self.driver, "_control", return_value=send), \
             patch.object(gemini_web.time, "monotonic", side_effect=[0, 0, 121]):
            with self.assertRaisesRegex(RuntimeError, "发送按钮未就绪"):
                self.driver._ready_send()
        send.click.assert_not_called()

    def test_composer_preview_ignores_other_page_images(self):
        self.page.locator.return_value.all.return_value = []
        self.assertFalse(self.driver._reference_previews_ready(1))
        self.assertIn("[data-reference-preview]", self.page.locator.call_args.args[0])
        self.assertNotEqual(self.page.locator.call_args.args[0], "img")

    def test_busy_or_failed_attachment_is_not_ready(self):
        card = MagicMock()
        card.is_visible.return_value = True
        images = MagicMock()
        images.count.return_value = 1
        images.first.evaluate.return_value = True
        errors = MagicMock()
        errors.all.return_value = []
        progress = MagicMock()
        busy = MagicMock()
        busy.is_visible.return_value = True
        progress.all.return_value = [busy]
        card.locator.side_effect = lambda selector: images if selector == "img" else errors if "alert" in selector else progress
        self.page.locator.return_value.all.return_value = [card]
        self.assertFalse(self.driver._reference_previews_ready(1))
        progress.all.return_value = []
        self.assertTrue(self.driver._reference_previews_ready(1))
        error = MagicMock()
        error.is_visible.return_value = True
        error.inner_text.return_value = "Upload failed"
        errors.all.return_value = [error]
        with self.assertRaisesRegex(RuntimeError, "上传失败"):
            self.driver._reference_previews_ready(1)

    def test_enabled_send_rechecks_reference_previews(self):
        self.driver._uploaded_reference_count = 1
        send = MagicMock()
        send.is_enabled.return_value = True
        with patch.object(self.driver, "_control", return_value=send), \
             patch.object(self.driver, "_reference_previews_ready", return_value=False) as previews, \
             patch.object(gemini_web.time, "monotonic", side_effect=[0, 0, 121]):
            with self.assertRaisesRegex(RuntimeError, "发送按钮未就绪"):
                self.driver._ready_send()
        previews.assert_called_once_with(1)
        send.click.assert_not_called()

    def test_image_elements_are_not_treated_as_video_results(self):
        responses = MagicMock()
        responses.count.return_value = 1
        responses.last.locator.return_value.all.return_value = []
        self.page.locator.return_value = responses
        self.assertEqual(self.driver._generated_videos(), [])
        responses.last.locator.assert_called_once_with("video")

    def test_video_requires_loaded_metadata_and_positive_duration(self):
        responses = MagicMock()
        responses.count.return_value = 1
        video = MagicMock()
        video.is_visible.return_value = True
        responses.last.locator.return_value.all.return_value = [video]
        self.page.locator.return_value = responses
        for ready, duration, accepted in [(0, 1, False), (1, 0, False), (1, 1, True)]:
            with self.subTest(ready=ready, duration=duration):
                video.evaluate.return_value = {"src": "blob:video", "ready": ready, "w": 32, "h": 48, "duration": duration}
                candidates = self.driver._generated_videos()
                self.assertEqual(bool(candidates), accepted)

    def test_corrupt_video_download_is_not_saved_or_reported_successful(self):
        self.record.update(media_type="video", status="submitted")
        video = MagicMock()
        video.hover.side_effect = AssertionError("兄弟播放遮罩拦截video本体，必须悬停父卡片")
        card = video.locator.return_value
        card.evaluate.return_value = "div"
        card.locator.return_value.count.return_value = 1
        button = MagicMock()
        button.is_visible.return_value = True
        card.get_by_role.return_value.all.return_value = [button]
        download = MagicMock()
        download.save_as.side_effect = lambda path: Path(path).write_bytes(b"corrupt video")
        pending = MagicMock()
        pending.value = download
        self.page.expect_download.return_value.__enter__.return_value = pending
        with self.assertRaises(ValueError):
            self.driver._download(self.record, video)
        card.hover.assert_called_once()
        video.hover.assert_not_called()
        self.assertEqual(self.record["status"], "download_failed")
        self.assertEqual(self.record["files"], [])
        self.assertFalse(any(call.args[0] in {"video_saved", "succeeded"} for call in self.emit.call_args_list))

    def test_video_success_is_emitted_only_after_chat_deletion(self):
        self.record["media_type"] = "video"
        operations = []

        def downloaded(record, *_):
            operations.append("download")
            record.update(status="succeeded", files=[{"path": "video.mp4", "media_type": "video"}])
            gemini_web.emit("video_saved", run_id=record["id"])

        def deleted(record):
            operations.append("delete")
            record["chat_cleanup"] = {"status": "deleted"}

        stack, stubs = self.generation_stubs(wait=downloaded, cleanup=deleted)
        with stack:
            self.driver.generate(self.record, 1)
        self.assertEqual(operations, ["download", "delete"])
        stubs["video_mode"].assert_called_once()
        stubs["image_mode"].assert_not_called()
        self.assertEqual([call.args[0] for call in self.emit.call_args_list][-2:], ["video_saved", "succeeded"])

    def test_video_cleanup_failure_preserves_file_and_prevents_success(self):
        self.record["media_type"] = "video"

        def downloaded(record, *_):
            record.update(status="succeeded", files=[{"path": "video.mp4", "media_type": "video"}])
            gemini_web.emit("video_saved", run_id=record["id"])

        stack, _ = self.generation_stubs(wait=downloaded, cleanup=gemini_web.ChatCleanupError("delete failed"))
        with stack, self.assertRaises(gemini_web.ChatCleanupError):
            self.driver.generate(self.record, 1)
        self.assertEqual(self.record["files"][0]["path"], "video.mp4")
        self.assertFalse(any(call.args[0] == "succeeded" for call in self.emit.call_args_list))

    def test_menu_dismissal_waits_for_delayed_close(self):
        menu = MagicMock()
        menu.is_visible.return_value = True
        self.page.get_by_role.return_value.all.side_effect = [[menu], [menu], []]
        with patch.object(gemini_web.time, "monotonic", return_value=0):
            self.driver._dismiss_menu()
        self.page.keyboard.press.assert_called_once_with("Escape")
        self.assertEqual(self.page.wait_for_timeout.call_count, 2)

    def test_menu_that_does_not_close_stops_later_actions(self):
        menu = MagicMock()
        menu.is_visible.return_value = True
        self.page.get_by_role.return_value.all.return_value = [menu]
        with patch.object(gemini_web.time, "monotonic", side_effect=[0, 0, 4]):
            with self.assertRaisesRegex(RuntimeError, "工具菜单未能关闭"):
                self.driver._dismiss_menu()

    def test_plain_share_is_not_an_export_menu_without_popup_semantics(self):
        scope = MagicMock()
        share = MagicMock()
        share.is_visible.return_value = True
        scope.get_by_role.return_value.all.return_value = [share]
        for name, popup, accepted in [("Share", None, False), ("Share", "menu", True),
                                      ("Share and export", None, True)]:
            with self.subTest(name=name, popup=popup):
                share.get_attribute.side_effect = lambda attribute: name if attribute == "aria-label" else popup
                self.assertEqual(self.driver._video_export_menu(scope) is share, accepted)

    @staticmethod
    def visible_locator(*elements):
        locator = MagicMock()
        locator.all.return_value = list(elements)
        for element in elements:
            element.is_visible.return_value = True
        return locator

    def test_exact_omni_video_introduction_is_accepted_before_continuing(self):
        title, model, start = [MagicMock() for _ in range(3)]
        controls = {"创作视频": self.visible_locator(title),
                    "Gemini Omni 加持": self.visible_locator(model)}
        self.page.get_by_text.side_effect = lambda text, **kwargs: controls[text]
        self.page.get_by_role.return_value = self.visible_locator(start)
        self.driver._accept_video_intro()
        start.click.assert_called_once_with()
        start.wait_for.assert_called_once_with(state="hidden", timeout=10000)
        self.page.get_by_text.assert_any_call("创作视频", exact=True)
        self.page.get_by_text.assert_any_call("Gemini Omni 加持", exact=True)
        self.page.get_by_role.assert_called_once_with("button", name="立即体验", exact=True)

    def test_generic_upgrade_or_incomplete_video_intro_is_never_accepted(self):
        for present in [set(), {"title", "start"}, {"model", "start"}, {"title", "model"}]:
            with self.subTest(present=present):
                title, model, start = [MagicMock() for _ in range(3)]
                controls = {"创作视频": self.visible_locator(title) if "title" in present else self.visible_locator(),
                            "Gemini Omni 加持": self.visible_locator(model) if "model" in present else self.visible_locator()}
                self.page.get_by_text.side_effect = lambda text, **kwargs: controls[text]
                self.page.get_by_role.return_value = self.visible_locator(start) if "start" in present else self.visible_locator()
                self.driver._accept_video_intro()
                start.click.assert_not_called()
                start.wait_for.assert_not_called()

    def test_trial_click_failure_does_not_submit_or_mark_submission_attempted(self):
        actual_ready_send = self.driver._ready_send
        stack, stubs = self.generation_stubs(ready=actual_ready_send)
        stubs["send"].click.side_effect = gemini_web.BrowserTimeoutError("overlay blocks trial")
        with stack, patch.object(self.driver, "_accept_video_intro"), \
             patch.object(gemini_web.time, "monotonic", side_effect=[0, 0, 121]):
            with self.assertRaisesRegex(RuntimeError, "发送按钮未就绪"):
                self.driver.generate(self.record, 1)
        stubs["send"].click.assert_called_once_with(trial=True, timeout=1000)
        stubs["editor"].press.assert_not_called()
        stubs["wait"].assert_not_called()
        stubs["cleanup"].assert_called_once()
        self.assertFalse(self.record.get("submission_attempted", False))
        self.assertNotIn(self.record["status"], {"submitting", "submitted", "succeeded"})

    def test_new_blank_chat_clears_old_draft_and_attachment_before_verification(self):
        new_chat, editor, attachment, remove = [MagicMock() for _ in range(4)]
        self.page.get_by_role.return_value = self.visible_locator(new_chat)
        attachment.is_visible.return_value = True
        remaining = [attachment]
        cards = MagicMock()
        cards.all.side_effect = lambda: list(remaining)
        messages = MagicMock()
        messages.count.return_value = 0
        self.page.locator.side_effect = lambda selector: cards if "data-reference-preview" in selector else messages
        attachment.get_by_role.return_value = self.visible_locator(remove)
        remove.click.side_effect = lambda: remaining.clear()
        with patch.object(self.driver, "_editor", return_value=editor):
            self.driver._new_chat(self.record)
        editor.fill.assert_called_once_with("")
        remove.click.assert_called_once_with()
        self.assertEqual(remaining, [])
        self.assertTrue(self.record["new_chat_verified"])
        self.assertFalse(self.record.get("submission_attempted", False))

    def test_old_draft_is_not_cleared_until_new_chat_identity_is_verified(self):
        new_chat, editor = MagicMock(), MagicMock()
        self.page.url = "https://gemini.google.com/app/existing-chat"
        self.page.get_by_role.return_value = self.visible_locator(new_chat)
        self.page.locator.return_value.count.return_value = 1
        with patch.object(self.driver, "_editor", return_value=editor), \
             patch.object(gemini_web.time, "monotonic", side_effect=[0, 0, 0, 0, 16]):
            with self.assertRaisesRegex(RuntimeError, "未能确认新建聊天为空白"):
                self.driver._new_chat(self.record)
        editor.fill.assert_not_called()
        self.assertFalse(self.record["new_chat_verified"])

    def test_already_selected_video_or_image_tool_is_not_toggled_off(self):
        for method, selected_name, expected in [
            ("_select_video_mode", "取消选择视频", "video_already_selected"),
            ("_select_image_mode", "取消选择图片", "image_already_selected"),
        ]:
            with self.subTest(tool=method):
                selected = MagicMock()
                self.page.get_by_role.side_effect = lambda role, name: (
                    self.visible_locator(selected) if name.search(selected_name) else self.visible_locator()
                )
                with patch.object(self.driver, "_editor") as editor:
                    self.assertEqual(getattr(self.driver, method)(), expected)
                selected.click.assert_not_called()
                editor.assert_not_called()
                self.page.keyboard.press.assert_not_called()


if __name__ == "__main__":
    unittest.main()
