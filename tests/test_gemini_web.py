"""Offline image-task lifecycle checks; no browser, account or network access."""

from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "gemini-browser" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import gemini_web
from local_state import RunStore


class GeminiWebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = RunStore(self.root / "outputs")
        self.record = self.store.create("画一只猫", self.root / "profile")
        self.session = MagicMock()
        self.session.page.url = "https://gemini.google.com/app"
        self.session.auth_state.return_value = "authenticated"
        self.session.diagnostics.return_value = {"diagnostics": "mock only"}
        self.driver = gemini_web.GeminiImages(self.session, self.store)
        self.page = self.session.page
        self.events = patch.object(gemini_web, "emit")
        self.emit = self.events.start()

    def tearDown(self):
        self.events.stop()
        self.temporary.cleanup()

    def owned_record(self, *, status="submitted"):
        self.record.update(status=status, chat_url="https://gemini.google.com/app/chat123",
                           new_chat_verified=True, chat_policy="new_then_delete",
                           submitted_prompt="请生成一张图片，而不是文字说明：\n\n画一只猫",
                           chat_cleanup={"status": "pending"})
        self.page.url = self.record["chat_url"]
        self.store.save(self.record)

    def generation_stubs(self, *, wait=None, cleanup=None):
        stack = ExitStack()
        self.page.locator.return_value.count.return_value = 0

        def new_chat(record):
            record.update(new_chat_verified=True, chat_policy="new_then_delete",
                          chat_cleanup={"status": "pending"})
            self.page.url = "https://gemini.google.com/app"

        def clean(record):
            record["chat_cleanup"] = {"status": "deleted"}
            self.store.save(record)

        stubs = {
            "new_chat": stack.enter_context(patch.object(self.driver, "_new_chat", side_effect=new_chat)),
            "editor": stack.enter_context(patch.object(self.driver, "_editor", return_value=MagicMock())),
            "mode": stack.enter_context(patch.object(self.driver, "_select_image_mode", return_value="image_button")),
            "baseline": stack.enter_context(patch.object(self.driver, "_image_sources", return_value=set())),
            "prompt_matches": stack.enter_context(patch.object(self.driver, "_prompt_matches", return_value=True)),
            "send": MagicMock(),
            "wait": stack.enter_context(patch.object(self.driver, "_wait_and_download", side_effect=wait)),
            "cleanup": stack.enter_context(patch.object(self.driver, "cleanup", side_effect=cleanup or clean)),
        }
        stack.enter_context(patch.object(self.driver, "_control", return_value=stubs["send"]))
        return stack, stubs

    def test_success_downloads_before_cleanup_and_reports_success_only_after_delete(self):
        operations = []

        def downloaded(record, *_):
            operations.append("download")
            record.update(status="succeeded", files=[{"path": "local-image.png"}])
            self.store.save(record)

        def deleted(record):
            operations.append("delete")
            record["chat_cleanup"] = {"status": "deleted"}
            self.store.save(record)

        stack, stubs = self.generation_stubs(wait=downloaded, cleanup=deleted)
        with stack:
            self.driver.generate(self.record, timeout=1)
        self.assertEqual(operations, ["download", "delete"])
        stubs["new_chat"].assert_called_once_with(self.record)
        stubs["send"].click.assert_called_once()
        success = [call for call in self.emit.call_args_list if call.args[0] == "succeeded"]
        self.assertEqual(len(success), 1)
        self.assertEqual(self.emit.call_args_list[-1].args[0], "succeeded")
        self.assertEqual(self.store.load(self.record["id"])["chat_cleanup"]["status"], "deleted")

    def test_ambiguous_click_is_not_retried_and_chat_cleanup_still_runs(self):
        stack, stubs = self.generation_stubs()

        def ambiguous_click(**kwargs):
            self.page.url = "https://gemini.google.com/app/chat123"
            raise RuntimeError("mock click timeout after dispatch")

        stubs["send"].click.side_effect = ambiguous_click
        with stack, self.assertRaisesRegex(RuntimeError, "mock click timeout"):
            self.driver.generate(self.record, timeout=1)
        stubs["send"].click.assert_called_once()
        stubs["editor"].return_value.press.assert_not_called()
        stubs["wait"].assert_not_called()
        stubs["cleanup"].assert_called_once_with(self.record)
        self.assertEqual(self.store.load(self.record["id"])["chat_url"], "https://gemini.google.com/app/chat123")
        self.assertFalse(any(call.args[0] == "succeeded" for call in self.emit.call_args_list))

    def test_generation_failure_still_cleans_its_chat(self):
        stack, stubs = self.generation_stubs(wait=RuntimeError("mock generation error"))
        with stack, self.assertRaisesRegex(RuntimeError, "mock generation error"):
            self.driver.generate(self.record, timeout=1)
        stubs["cleanup"].assert_called_once_with(self.record)
        stubs["send"].click.assert_called_once()
        self.assertFalse(any(call.args[0] == "succeeded" for call in self.emit.call_args_list))

    def test_keyboard_interrupt_still_attempts_chat_cleanup(self):
        stack, stubs = self.generation_stubs(wait=KeyboardInterrupt())
        with stack, self.assertRaises(KeyboardInterrupt):
            self.driver.generate(self.record, timeout=1)
        stubs["cleanup"].assert_called_once_with(self.record)
        stubs["send"].click.assert_called_once()

    def test_cleanup_failure_does_not_remove_downloaded_files_or_emit_success(self):
        image = self.store.path_for(self.record["id"]).parent / "saved-image.png"
        image.write_bytes(b"an already saved file is retained")

        def downloaded(record, *_):
            record.update(status="succeeded", files=[{"path": str(image)}])
            self.store.save(record)

        stack, stubs = self.generation_stubs(wait=downloaded, cleanup=gemini_web.ChatCleanupError("mock delete failed"))
        with stack, self.assertRaisesRegex(gemini_web.ChatCleanupError, "mock delete failed"):
            self.driver.generate(self.record, timeout=1)
        self.assertTrue(image.is_file())
        self.assertEqual(self.record["status"], "succeeded")
        self.assertEqual(self.record["files"], [{"path": str(image)}])
        self.assertFalse(any(call.args[0] == "succeeded" for call in self.emit.call_args_list))

    def test_unverified_new_chat_never_reaches_composer_or_submission(self):
        with patch.object(self.driver, "_new_chat", side_effect=RuntimeError("existing chat is not blank")), \
             patch.object(self.driver, "_editor") as editor, \
             patch.object(self.driver, "_control") as control, \
             patch.object(self.driver, "cleanup") as cleanup:
            with self.assertRaisesRegex(RuntimeError, "not blank"):
                self.driver.generate(self.record, timeout=1)
        editor.assert_not_called()
        control.assert_not_called()
        cleanup.assert_called_once_with(self.record)

    def test_already_deleted_cleanup_never_touches_the_browser(self):
        self.owned_record(status="succeeded")
        self.record["chat_cleanup"] = {"status": "deleted"}
        with patch.object(self.driver, "_delete_task_chat") as delete:
            self.driver.cleanup(self.record)
        delete.assert_not_called()
        self.session.navigate.assert_not_called()
        self.page.get_by_role.assert_not_called()

    def test_pre_submission_without_chat_requires_no_deletion(self):
        with patch.object(self.driver, "_delete_task_chat") as delete:
            self.driver.cleanup(self.record)
        self.assertEqual(self.record["chat_cleanup"]["status"], "not_created")
        delete.assert_not_called()

    def test_submitted_without_chat_url_is_not_falsely_marked_deleted(self):
        self.record.update(status="submitted", new_chat_verified=True)
        with patch.object(self.driver, "_delete_task_chat") as delete:
            with self.assertRaises(gemini_web.ChatCleanupError):
                self.driver.cleanup(self.record)
        self.assertEqual(self.record["chat_cleanup"]["status"], "failed")
        delete.assert_not_called()

    def test_login_loss_after_submission_without_url_still_requires_cleanup(self):
        self.record.update(status="submitted", new_chat_verified=True,
                           submitted_prompt="请生成一张图片，而不是文字说明：\n\n画一只猫")
        self.page.url = "https://gemini.google.com/app"
        self.driver._record_error(self.record, gemini_web.LoginRequired("mock session expired"))
        with patch.object(self.driver, "_delete_task_chat") as delete:
            with self.assertRaises(gemini_web.ChatCleanupError):
                self.driver.cleanup(self.record)
        self.assertEqual(self.record["chat_cleanup"]["status"], "failed")
        delete.assert_not_called()

    def test_cleanup_failure_is_saved_separately_from_download_result(self):
        self.owned_record(status="succeeded")
        self.record["files"] = [{"path": "local-image.png"}]
        with patch.object(self.driver, "_delete_task_chat", side_effect=RuntimeError("not allowed")):
            with self.assertRaises(gemini_web.ChatCleanupError):
                self.driver.cleanup(self.record)
        saved = self.store.load(self.record["id"])
        self.assertEqual(saved["status"], "succeeded")
        self.assertEqual(saved["files"], [{"path": "local-image.png"}])
        self.assertEqual(saved["chat_cleanup"]["status"], "failed")

    def test_cleanup_rejects_offsite_or_generic_chat_urls_before_deleting(self):
        for url in ["https://gemini.google.com/app", "http://gemini.google.com/app/chat123",
                    "https://gemini.google.com.attacker.example/app/chat123"]:
            with self.subTest(url=url):
                self.owned_record()
                self.record["chat_url"] = url
                with patch.object(self.driver, "_delete_task_chat") as delete:
                    with self.assertRaises(gemini_web.ChatCleanupError):
                        self.driver.cleanup(self.record)
                delete.assert_not_called()

    def test_validate_task_chat_refuses_a_different_current_chat(self):
        self.owned_record()
        self.page.url = "https://gemini.google.com/app/another-chat"
        with self.assertRaises((RuntimeError, ValueError)):
            self.driver._validate_task_chat(self.record)
        self.page.get_by_role.assert_not_called()

    def sidebar_menu_fixture(self, *, own_button=None):
        self.owned_record()
        self.page.get_by_role.return_value.all.return_value = []
        link, own_row, shared_list, unrelated_button = [MagicMock() for _ in range(4)]
        self.page.locator.return_value = link
        link.count.return_value = 1
        link.evaluate.return_value = ["/app/chat123"]
        link.get_by_role.return_value.all.return_value = []
        link.locator.return_value = own_row
        own_row.evaluate.return_value = [self.record["chat_url"]]
        own_row.get_by_role.return_value.all.return_value = [own_button] if own_button else []
        own_row.locator.return_value = shared_list
        shared_list.evaluate.return_value = ["/app/chat123", "/app/unrelated-chat"]
        shared_list.get_by_role.return_value.all.return_value = [unrelated_button]
        unrelated_button.is_visible.return_value = True
        if own_button is not None:
            own_button.is_visible.return_value = True
        return link, own_row, shared_list, unrelated_button

    def test_chat_without_own_menu_never_clicks_neighbor_menu_in_shared_list(self):
        link, _, shared_list, unrelated_button = self.sidebar_menu_fixture()
        with self.assertRaisesRegex(gemini_web.ChatCleanupError, "没有找到本任务聊天的菜单按钮"):
            self.driver._open_chat_menu(self.record)
        link.hover.assert_called_once()
        shared_list.evaluate.assert_called_once()
        shared_list.get_by_role.assert_not_called()
        unrelated_button.click.assert_not_called()

    def test_sidebar_menu_with_unique_target_row_still_opens_its_own_menu(self):
        own_button = MagicMock()
        _, _, shared_list, unrelated_button = self.sidebar_menu_fixture(own_button=own_button)
        self.driver._open_chat_menu(self.record)
        own_button.click.assert_called_once()
        shared_list.evaluate.assert_not_called()
        unrelated_button.click.assert_not_called()

    def test_unique_chat_header_menu_does_not_require_sidebar_lookup(self):
        header = MagicMock()
        header.is_visible.return_value = True
        menu_locator, header_locator = MagicMock(), MagicMock()
        menu_locator.all.return_value = []
        header_locator.all.return_value = [header]
        self.page.get_by_role.side_effect = lambda role, **kwargs: (
            menu_locator if role == "menu" else header_locator
        )
        self.driver._open_chat_menu(self.record)
        header.click.assert_called_once()
        self.page.locator.assert_not_called()

    def test_deleted_chat_requires_explicit_missing_notice_without_user_query(self):
        self.owned_record(status="succeeded")
        self.page.url = "https://gemini.google.com/app"
        self.page.locator.return_value.count.return_value = 0
        with patch.object(self.driver, "_missing_chat_notice", return_value=True), \
             patch.object(self.driver, "_prompt_matches", return_value=False):
            evidence = self.driver._verify_chat_deleted(self.record)
        self.assertEqual(evidence, {"evidence": "chat_not_found"})
        self.session.navigate.assert_called_once_with(self.record["chat_url"])
        self.session.ensure_login.assert_called_once_with(60)

    def test_disappeared_sidebar_without_missing_notice_is_not_deletion_proof(self):
        self.owned_record(status="succeeded")
        self.page.url = "https://gemini.google.com/app"
        self.page.locator.return_value.count.return_value = 0
        with patch.object(gemini_web.time, "monotonic", side_effect=[0, 0, 0, 0, 31]), \
             patch.object(self.driver, "_missing_chat_notice", return_value=False), \
             patch.object(self.driver, "_prompt_matches", return_value=False):
            with self.assertRaisesRegex(gemini_web.ChatCleanupError, "明确的聊天不存在提示"):
                self.driver._verify_chat_deleted(self.record)

    def test_unverified_page_url_is_never_adopted_as_task_chat(self):
        self.record["status"] = "submitting"
        self.page.url = "https://gemini.google.com/app/some-old-chat"
        self.driver._remember_url(self.record)
        self.assertIsNone(self.record["chat_url"])

    def test_timeout_preserves_task_chat_url_in_durable_record(self):
        self.owned_record()
        with patch.object(gemini_web.time, "monotonic", side_effect=[0, 0, 0, 0, 2]), \
             patch.object(self.driver, "_blocked_reason", return_value=None), \
             patch.object(self.driver, "_generated_images", return_value=[]), \
             patch.object(self.driver, "_control", return_value=None):
            with self.assertRaises(gemini_web.GenerationTimeout):
                self.driver._wait_and_download(self.record, timeout=1, baseline=set())
        restored = self.store.load(self.record["id"])
        self.assertEqual(restored["status"], "timed_out")
        self.assertEqual(restored["chat_url"], "https://gemini.google.com/app/chat123")

    def test_corrupt_native_download_is_not_reported_as_success(self):
        self.owned_record()
        image = MagicMock()
        button = MagicMock()
        button.is_visible.return_value = True
        card = image.locator.return_value
        card.evaluate.return_value = "div"
        card.locator.return_value.evaluate_all.return_value = 1
        card.get_by_role.return_value.all.return_value = [button]
        download = MagicMock()
        download.save_as.side_effect = lambda path: Path(path).write_bytes(b"broken image payload")
        pending = MagicMock()
        pending.value = download
        self.page.expect_download.return_value.__enter__.return_value = pending
        with self.assertRaisesRegex(ValueError, "图片校验失败"):
            self.driver._download(self.record, image)
        button.click.assert_called_once()
        self.assertEqual(self.store.load(self.record["id"])["status"], "download_failed")
        self.assertEqual(self.record["files"], [])
        self.assertFalse(any(call.args[0] == "succeeded" for call in self.emit.call_args_list))

    def test_ambiguous_download_buttons_fail_without_global_fallback(self):
        image = MagicMock()
        card = image.locator.return_value
        card.evaluate.return_value = "div"
        card.locator.return_value.evaluate_all.return_value = 1
        buttons = [MagicMock(), MagicMock()]
        for button in buttons:
            button.is_visible.return_value = True
        card.get_by_role.return_value.all.return_value = buttons
        with self.assertRaisesRegex(RuntimeError, "没有找到对应媒体的原生下载按钮"):
            self.driver._download(self.record, image)
        self.page.get_by_role.assert_not_called()
        self.page.expect_download.assert_not_called()
        for button in buttons:
            button.click.assert_not_called()

    def test_multiple_assistant_responses_are_not_silently_reused(self):
        class TwoResponses:
            def count(self):
                return 2

            @property
            def last(self):
                raise AssertionError("多条回复时不得选择最后一条")

        self.page.locator.return_value = TwoResponses()
        with self.assertRaisesRegex(RuntimeError, "多条回复"):
            self.driver._generated_images(set())
        self.page.expect_download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
