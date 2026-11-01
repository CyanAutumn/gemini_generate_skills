"""Gemini images: a fresh chat per task, native download, then targeted deletion."""

from __future__ import annotations

import re
import time
from contextlib import suppress
from urllib.parse import urlsplit

from browser_session import GEMINI_URL, LoginRequired, emit, redact
from local_state import utc_now, verify_image

IMAGE_MODE = re.compile(
    r"^(Create images?|Generate images?|生成图片|生成图像|创建图片|建立圖像|製作圖像|制作图片)(\s.*)?$", re.I
)
TOOLS = re.compile(r"^(Tools|工具)(\s.*)?$", re.I)
SEND = re.compile(r"^(Send( message)?|Submit|发送(消息)?|提交|傳送|送出)$", re.I)
STOP = re.compile(r"^(Stop( response| generating)?|停止(回答|回复|生成)?|停止回覆)$", re.I)
DOWNLOAD = re.compile(r"download|下载|下載", re.I)
NEW_CHAT = re.compile(r"^(New chat|Start (?:a )?new chat|发起新对话|新建聊天|新聊天|新对话|開始新對話)(\s.*)?$", re.I)
CHAT_MENU = re.compile(r"打开对话操作菜单|開啟對話操作選單|Open (?:chat|conversation) (?:actions )?menu", re.I)
DELETE = re.compile(r"^(Delete(?: chat| conversation)?|删除(?:对话|聊天)?|刪除(?:對話|聊天)?)$", re.I)
MORE = re.compile(r"more|更多选项|更多選項|更多", re.I)
MISSING_CHAT = re.compile(
    r"(?:对话|聊天|對話|conversation|chat).{0,100}(?:不存在|已被删除|已刪除|does(?:n['’]t| not) exist|has been deleted|not found)", re.I
)
BLOCKED = re.compile(
    r"limit (?:has been )?reached|reached (?:your |the )?.{0,45}limit|"
    r"out of (?:image |daily )?(?:generations|credits)|try again tomorrow|"
    r"image generation.{0,50}(?:unavailable|not available)|"
    r"(?:已达|达到|超出).{0,35}(?:上限|限额)|(?:次数|额度).{0,20}(?:用完|耗尽)|"
    r"(?:无法|不能|暂不支持).{0,15}(?:生成图片|生成图像)", re.I
)


class GenerationTimeout(RuntimeError):
    pass


class ChatCleanupError(RuntimeError):
    pass


class GeminiImages:
    def __init__(self, session, store):
        self.session = session
        self.store = store
        self.page = session.page

    def _first_visible(self, locator):
        for item in locator.all():
            if item.is_visible():
                return item
        return None

    def _control(self, pattern, scope=None):
        scope = scope if scope is not None else self.page
        for role in ("button", "menuitem", "option"):
            control = self._first_visible(scope.get_by_role(role, name=pattern))
            if control is not None:
                return control
        return None

    def _editor(self):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            editor = self._first_visible(self.page.get_by_role("textbox"))
            if editor is None:
                editor = self._first_visible(self.page.locator('[contenteditable="true"], textarea'))
            if editor is not None:
                return editor
            self.page.wait_for_timeout(500)
        raise RuntimeError("没有找到 Gemini 提示词输入框。请检查当前页面或登录状态。")

    def _select_image_mode(self):
        control = self._control(IMAGE_MODE)
        if control is not None:
            control.click()
            return "image_button"
        tools = self._control(TOOLS)
        if tools is not None:
            tools.click()
            control = self._control(IMAGE_MODE)
            if control is not None:
                control.click()
                return "tools_image_mode"
            self.page.keyboard.press("Escape")
        # Gemini also accepts an explicit image request in the normal composer.
        return "prompt_request"

    @staticmethod
    def _is_chat_url(url):
        parts = urlsplit(url)
        return (parts.scheme == "https" and parts.hostname == "gemini.google.com"
                and re.fullmatch(r"/app/[A-Za-z0-9_-]+/?", parts.path) is not None)

    def _remember_url(self, record):
        # Capture once, only after a verified fresh-chat submission. Never replace
        # the owned URL if somebody navigates to a different conversation.
        if (record.get("new_chat_verified") and not record.get("chat_url")
                and record.get("status") in {"submitting", "submitted", "downloading", "download_failed", "timed_out", "blocked"}
                and self._is_chat_url(self.page.url) and self._prompt_matches(record)):
            record["chat_url"] = self.page.url
            self.store.save(record)
            emit("chat_saved", run_id=record["id"], chat_url=record["chat_url"])

    @staticmethod
    def _normalized(text):
        return re.sub(r"\s+", "", text or "")

    def _prompt_matches(self, record):
        queries = self.page.locator('user-query, [data-message-author-role="user"]')
        expected = self._normalized(record.get("submitted_prompt"))
        return (bool(expected) and queries.count() == 1
                and expected in self._normalized(queries.first.text_content()))

    def _new_chat(self, record):
        record["chat_policy"] = "new_then_delete"
        record["new_chat_verified"] = False
        record["chat_cleanup"] = {"status": "pending"}
        self.store.save(record)
        self.session.navigate(GEMINI_URL)
        deadline = time.monotonic() + 30
        new_chat = None
        while time.monotonic() < deadline:
            for role in ("link", "button"):
                new_chat = self._first_visible(self.page.get_by_role(role, name=NEW_CHAT))
                if new_chat is not None:
                    break
            if new_chat is not None:
                break
            self.page.wait_for_timeout(500)
        if new_chat is None:
            raise RuntimeError("没有找到新建聊天入口，已停止；不会在已有聊天中提交。")
        new_chat.click()
        editor = self._editor()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = urlsplit(self.page.url)
            messages = self.page.locator('user-query, model-response, [data-message-author-role]')
            if (current.hostname == urlsplit(GEMINI_URL).hostname and current.path.rstrip("/") == "/app"
                    and not messages.count()):
                break
            self.page.wait_for_timeout(250)
        else:
            raise RuntimeError("未能确认新建聊天为空白，已停止；不会复用原聊天。")
        if (editor.text_content() or "").strip():
            raise RuntimeError("新聊天输入框已有内容，已停止提交。")
        record["new_chat_verified"] = True
        self.store.save(record)
        emit("new_chat", run_id=record["id"], message="已确认空白新聊天，本任务结束后删除。")

    def _image_sources(self):
        return set(self.page.locator("img").evaluate_all("els => els.map(e => e.currentSrc || e.src)"))

    def generate(self, record, timeout):
        failure = None
        try:
            self._new_chat(record)
            record["image_mode"] = self._select_image_mode()
            editor = self._editor()
            submitted_prompt = "请生成一张图片，而不是文字说明：\n\n" + record["prompt"]
            editor.fill(submitted_prompt)
            baseline = self._image_sources()
            send = self._control(SEND)
            # Save before the only non-idempotent UI action. Never auto-retry this action.
            record["submitted_prompt"] = submitted_prompt
            record["submission_attempted"] = True
            record["status"] = "submitting"
            self.store.save(record)
            emit("submitting", run_id=record["id"], image_mode=record["image_mode"])
            if send is not None:
                send.click(timeout=15000)
            else:
                editor.press("Enter", timeout=15000)
            record["status"] = "submitted"
            self._remember_url(record)
            self.store.save(record)
            self._wait_and_download(record, timeout, baseline)
        except BaseException as exc:
            failure = exc
            self._record_error(record, exc)
            raise
        finally:
            try:
                self.cleanup(record)
            except BaseException as cleanup_exc:
                if failure is None:
                    raise
                emit("cleanup_failed", run_id=record["id"], message=redact(str(cleanup_exc)),
                     action="请使用 cleanup 补删本任务聊天，不要复用原聊天。")
        emit("succeeded", run_id=record["id"], files=record["files"], chat_cleanup=record["chat_cleanup"])

    def cleanup(self, record):
        state = record.get("chat_cleanup", {}).get("status")
        if state in {"deleted", "not_created"}:
            return
        try:
            self._remember_url(record)
            if not record.get("chat_url"):
                if (record.get("status") in {"prepared", "waiting_for_login", "failed_before_submit"}
                        and not record.get("submission_attempted") and not record.get("submitted_prompt")):
                    record["chat_cleanup"] = {"status": "not_created", "updated_at": utc_now()}
                    self.store.save(record)
                    return
                raise ChatCleanupError("已尝试提交但没有取得聊天链接，无法安全定位清理目标。请人工检查本次新聊天。")
            if not self._is_chat_url(record["chat_url"]):
                raise ChatCleanupError("清理目标不是 Gemini 独立聊天地址，拒绝删除。")
            record["chat_cleanup"] = {"status": "deleting", "updated_at": utc_now()}
            self.store.save(record)
            evidence = self._delete_task_chat(record)
            record["chat_cleanup"] = {"status": "deleted", "updated_at": utc_now(),
                                      **(evidence if isinstance(evidence, dict) else {})}
            self.store.save(record)
            emit("chat_deleted", run_id=record["id"], message="本任务聊天已删除，本地图片和记录保留。")
        except BaseException as exc:
            record["chat_cleanup"] = {"status": "failed", "error": redact(str(exc)), "updated_at": utc_now()}
            self.store.save(record)
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise ChatCleanupError("任务聊天清理未完成：" + redact(str(exc))) from exc

    def _validate_task_chat(self, record):
        if not self._same_chat(self.page.url, record["chat_url"]):
            raise ChatCleanupError("当前页面与任务聊天地址不符，拒绝删除。当前路径="
                                   + urlsplit(self.page.url).path + "，目标路径=" + urlsplit(record["chat_url"]).path)
        if self.session.auth_state() != "authenticated":
            raise ChatCleanupError("登录状态未确认，无法删除任务聊天。")
        # Legacy runs predate the explicit ownership marker. They are deletable
        # only when their unique submitted prompt matches this one-turn chat.
        if not record.get("new_chat_verified"):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self._prompt_matches(record):
                    return
                self.page.wait_for_timeout(500)
            raise ChatCleanupError("无法确认旧记录中的提示词属于当前聊天，拒绝删除。")

    @staticmethod
    def _same_chat(first, second):
        a, b = urlsplit(first), urlsplit(second)
        return (a.scheme, a.hostname, a.path.rstrip("/")) == (b.scheme, b.hostname, b.path.rstrip("/"))

    def _open_chat_menu(self, record):
        controls = [b for b in self.page.get_by_role("button", name=CHAT_MENU).all() if b.is_visible()]
        if len(controls) == 1:
            controls[0].click()
            return
        path = urlsplit(record["chat_url"]).path.rstrip("/")
        row = self.page.locator(f'a[href="{path}"], a[href="{record["chat_url"]}"]')
        if row.count() != 1:
            raise ChatCleanupError("没有唯一匹配本任务的聊天行，拒绝操作其他聊天。")
        row.hover()
        container = row
        for _ in range(3):
            buttons = [b for b in container.get_by_role("button", name=MORE).all() if b.is_visible()]
            if len(buttons) == 1:
                buttons[0].click()
                return
            if len(buttons) > 1:
                break
            container = container.locator("..")
        raise ChatCleanupError("没有找到本任务聊天的菜单按钮。")

    def _delete_task_chat(self, record):
        if not self._same_chat(self.page.url, record["chat_url"]):
            self.session.navigate(record["chat_url"])
        self.session.ensure_login(60)
        self.page = self.session.page
        # Give a newly opened chat time to render its own header controls.
        self._editor()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            missing = self._missing_chat_notice()
            if missing and not self.page.locator('user-query, [data-message-author-role="user"]').count():
                return {"evidence": "chat_not_found", "already_absent": True}
            if self._same_chat(self.page.url, record["chat_url"]) and self._prompt_matches(record):
                break
            self.page.wait_for_timeout(500)
        else:
            raise ChatCleanupError("未能确认任务聊天已加载或已不存在，拒绝删除其他聊天。")
        self._validate_task_chat(record)
        self._open_chat_menu(record)
        menus = self.page.get_by_role("menu")
        menus.last.wait_for(state="visible", timeout=10000)
        delete = self._control(DELETE, menus.last)
        if delete is None:
            raise ChatCleanupError("本任务菜单没有删除入口，账号可能不支持删除。")
        delete.click()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            dialogs = [d for d in self.page.get_by_role("dialog").all() if d.is_visible()]
            if dialogs:
                if len(dialogs) != 1:
                    raise ChatCleanupError("删除确认窗口不唯一，已停止。")
                confirm = self._control(DELETE, dialogs[0])
                if confirm is None:
                    raise ChatCleanupError("未找到当前聊天的删除确认按钮。")
                confirm.click()
                break
            if not self._same_chat(self.page.url, record["chat_url"]):
                break
            self.page.wait_for_timeout(250)
        return self._verify_chat_deleted(record)

    def _missing_chat_notice(self):
        notices = self.page.locator('[role="alert"], [role="status"], mat-snack-bar-container')
        for notice in notices.all():
            if notice.is_visible() and MISSING_CHAT.search(notice.inner_text()):
                return True
        return False

    def _verify_chat_deleted(self, record):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if not self._same_chat(self.page.url, record["chat_url"]):
                break
            self.page.wait_for_timeout(500)
        else:
            raise ChatCleanupError("删除后仍停留在任务聊天，尚未确认清理成功。")
        # Revisit only to verify absence, never to continue or generate. A hidden
        # sidebar, delayed history or pagination is not evidence of deletion.
        self.session.navigate(record["chat_url"])
        self.session.ensure_login(60)
        self.page = self.session.page
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._missing_chat_notice() and not self.page.locator('user-query, [data-message-author-role="user"]').count():
                return {"evidence": "chat_not_found"}
            if self._prompt_matches(record):
                raise ChatCleanupError("删除后仍能打开本任务聊天，未确认清理成功。")
            self.page.wait_for_timeout(500)
        raise ChatCleanupError("没有取得网页明确的聊天不存在提示，不能确认删除成功。")

    def _generated_images(self, baseline):
        responses = self.page.locator('model-response, [data-message-author-role="assistant"]')
        response_count = responses.count()
        if not response_count:
            return []
        if response_count != 1:
            raise RuntimeError("原聊天包含多条回复，无法唯一确认本任务的图片，已停止下载。请人工核对原聊天，勿重新提交。")
        scope = responses.last
        candidates = []
        for image in scope.locator("img").all():
            if not image.is_visible():
                continue
            info = image.evaluate("e => ({src:e.currentSrc || e.src, complete:e.complete, w:e.naturalWidth, h:e.naturalHeight})")
            if info["src"] not in baseline and info["complete"] and info["w"] >= 256 and info["h"] >= 256:
                candidates.append((image, info["src"]))
        return candidates

    def _blocked_reason(self):
        responses = self.page.locator('model-response, [data-message-author-role="assistant"], [role="alert"]')
        text = "\n".join(x.inner_text() for x in responses.all() if x.is_visible())
        for line in text.splitlines():
            if BLOCKED.search(line):
                return redact(line.strip())
        return None

    def _wait_and_download(self, record, timeout, baseline):
        deadline = time.monotonic() + timeout
        next_report = 0
        previous_source = None
        stable = 0
        while time.monotonic() < deadline:
            self._remember_url(record)
            if self.session.auth_state() == "login_required":
                raise LoginRequired("会话已失效；本任务停止，登录后可用 cleanup 补删任务聊天。")
            if record.get("chat_url") and not self._same_chat(self.page.url, record["chat_url"]):
                raise RuntimeError("当前页面已离开本任务聊天，停止生成等待。")
            blocked = self._blocked_reason()
            if blocked:
                record["status"] = "blocked"
                record["error"] = blocked
                self.store.save(record)
                raise RuntimeError("Gemini 页面反馈：" + blocked)
            candidates = self._generated_images(baseline)
            stopping = self._control(STOP)
            if candidates and stopping is None:
                source = candidates[0][1]
                stable = stable + 1 if source == previous_source else 1
                previous_source = source
                if stable >= 2:
                    self._download(record, candidates[0][0])
                    return
            else:
                stable = 0
            if time.monotonic() >= next_report:
                emit("generating", run_id=record["id"], chat_url=record.get("chat_url"),
                     message="等待 Gemini 图片生成；不会重新提交提示词。")
                next_report = time.monotonic() + 15
            self.page.wait_for_timeout(1500)
        record["status"] = "timed_out"
        self.store.save(record)
        raise GenerationTimeout("等待图片超时，任务结束并尝试删除本次聊天；不会复用聊天或自动重新生成。")

    def _download(self, record, image):
        image.hover()
        self.page.wait_for_timeout(500)
        button = None
        container = image
        for _ in range(7):
            container = container.locator("..")
            # Do not widen a card search into the whole chat or another message.
            tag = container.evaluate("e => e.tagName.toLowerCase()")
            images = container.locator("img").evaluate_all(
                "els => els.filter(e => e.naturalWidth >= 256 && e.naturalHeight >= 256).length"
            )
            if tag in {"body", "html", "main", "model-response"} or images > 1:
                break
            buttons = [b for b in container.get_by_role("button", name=DOWNLOAD).all() if b.is_visible()]
            if len(buttons) == 1:
                button = buttons[0]
                break
            if len(buttons) > 1:
                break
        if button is None:
            record["status"] = "download_failed"
            self.store.save(record)
            raise RuntimeError("图片已出现，但没有找到原生下载按钮。任务结束并尝试删除本次聊天。")
        record["status"] = "downloading"
        self.store.save(record)
        emit("downloading", run_id=record["id"])
        try:
            with self.page.expect_download(timeout=60000) as pending:
                button.click()
            download = pending.value
            destination = self.store.path_for(record["id"]).parent / "image.download"
            download.save_as(str(destination))
            info = verify_image(destination)
            extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif"}[info["format"]]
            final_path = destination.with_name("image" + extension)
            destination.replace(final_path)
            info["path"] = str(final_path.resolve())
            record["files"] = [info]
            record["status"] = "succeeded"
            record.pop("error", None)
            self.store.save(record)
            emit("image_saved", run_id=record["id"], files=record["files"])
        except BaseException:
            record["status"] = "download_failed"
            self.store.save(record)
            raise

    def _record_error(self, record, exc):
        with suppress(Exception):
            self._remember_url(record)
            record["error"] = redact(str(exc))
            if isinstance(exc, LoginRequired):
                record["status"] = "waiting_for_login"
            elif record["status"] in {"prepared", "waiting_for_login"}:
                record["status"] = "failed_before_submit"
            self.store.save(record)
            # Failure evidence is local; it must never include Google login forms.
            record["diagnostics"] = self.session.diagnostics(self.store.path_for(record["id"]).parent / "diagnostics")
            self.store.save(record)
